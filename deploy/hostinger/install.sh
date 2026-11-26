#!/usr/bin/env bash
# Meltice GPS Tracker - native install on a Hostinger VPS (Ubuntu 22.04/24.04).
# Run from the unpacked project folder:   sudo bash deploy/hostinger/install.sh
#
# Database (DB_MODE):
#   docker   (default) SQL Server 2022 Express in Docker on this VPS, 127.0.0.1:1433 (install_sqlserver.sh)
#   external an existing SQL Server: DB_HOST=... DB_NAME=... DB_USER=... DB_PASSWORD=... [DB_PORT=1433]
#            add DB_ADMIN_USER=sa DB_ADMIN_PASSWORD=... to let the script create the database and login
#   sqlite   single-file database, quick trial only
#
# What it does (idempotent, safe to re-run):
#   - creates service account 'fleet', /srv/meltice-gps-tracker, /var/lib/meltice-gps-tracker, /var/log/meltice-gps-tracker
#   - installs Microsoft ODBC Driver 18, copies the app, creates a virtualenv, installs requirements
#   - creates the SQL Server database + least-privilege login 'fleet_app' (db_owner of that database only)
#   - creates /etc/meltice-gps-tracker/fleet.env (only if missing) with a random secret key
#   - runs migrations, collectstatic, creates the admin user
#   - installs fleet-receiver.service + gunicorn-fleet.service
#   - installs a NEW Nginx server block for fleet.melticetech.com (backs up /etc/nginx first, runs nginx -t)
# What it does NOT do (do these consciously, see docs/HOSTINGER_DEPLOYMENT.md):
#   - enable/reset UFW, change other Nginx sites, request TLS certificates, open the Hostinger panel firewall
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../.." && pwd)"
APP=/srv/meltice-gps-tracker/app
VENV=/srv/meltice-gps-tracker/venv
ENVF=/etc/meltice-gps-tracker/fleet.env
WEB_DOMAIN="${WEB_DOMAIN:-fleet.melticetech.com}"
GPS_DOMAIN="${GPS_DOMAIN:-gps.melticetech.com}"
DB_MODE="${DB_MODE:-docker}"
SECRETS=/etc/meltice-gps-tracker/mssql.env

[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
echo "==> Pre-flight: ports already in use (5027 / 9040)"
if [[ -f /etc/systemd/system/fleet-receiver.service ]]; then
  echo "   existing Meltice GPS Tracker install detected - upgrading in place"
elif command -v ss >/dev/null && ss -ltnp | grep -E ":(5027|9040)\b"; then
  echo "!! Port 5027 or 9040 is used by another program - stop and investigate before continuing."; exit 1
else
  echo "   free"
fi

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx sqlite3 rsync iproute2 sudo curl gnupg lsb-release >/dev/null

if [[ $DB_MODE != sqlite ]]; then
  echo "==> Microsoft ODBC Driver 18 for SQL Server"
  if ! odbcinst -q -d 2>/dev/null | grep -q "ODBC Driver 18 for SQL Server"; then
    . /etc/os-release
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor --yes -o /usr/share/keyrings/microsoft-prod.gpg
    curl -fsSL "https://packages.microsoft.com/config/ubuntu/${VERSION_ID}/prod.list" -o /etc/apt/sources.list.d/mssql-release.list
    apt-get update -qq
    ACCEPT_EULA=Y apt-get install -y -qq msodbcsql18 unixodbc-dev >/dev/null
  fi
  odbcinst -q -d | grep "SQL Server"
fi
if [[ $DB_MODE == docker ]]; then
  bash "$SRC/deploy/hostinger/install_sqlserver.sh"
elif [[ $DB_MODE == external ]]; then
  for v in DB_HOST DB_NAME DB_USER DB_PASSWORD; do [[ -n ${!v:-} ]] || { echo "!! DB_MODE=external needs $v"; exit 1; }; done
fi

echo "==> Service account and folders"
id fleet >/dev/null 2>&1 || useradd --system --home /srv/meltice-gps-tracker --shell /usr/sbin/nologin fleet
install -d -o fleet -g fleet -m 750 /srv/meltice-gps-tracker /var/lib/meltice-gps-tracker /var/log/meltice-gps-tracker
install -d -o root -g fleet -m 750 /etc/meltice-gps-tracker

echo "==> Application code -> $APP"
rsync -a --delete --exclude ".env" --exclude "data/" --exclude "logs/" --exclude "staticfiles/" --exclude "__pycache__/" "$SRC/" "$APP/"
chown -R fleet:fleet "$APP"

echo "==> Virtualenv"
[[ -d $VENV ]] || sudo -u fleet python3 -m venv "$VENV"
REQ=requirements.txt
sudo -u fleet "$VENV/bin/pip" install -q --upgrade pip
sudo -u fleet "$VENV/bin/pip" install -q -r "$APP/$REQ"

if [[ ! -f $ENVF ]]; then
  echo "==> Creating $ENVF"
  SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")
  ADMINPW=$(python3 -c "import secrets; print(secrets.token_urlsafe(14))")
  sed -e "s|^FLEET_SECRET_KEY=.*|FLEET_SECRET_KEY=$SECRET|" \
      -e "s|^FLEET_ADMIN_PASSWORD=.*|FLEET_ADMIN_PASSWORD=$ADMINPW|" \
      -e "s|^FLEET_ENVIRONMENT_LABEL=.*|FLEET_ENVIRONMENT_LABEL=Hostinger|" \
      -e "s|^FLEET_DEBUG=.*|FLEET_DEBUG=false|" \
      -e "s|^FLEET_ALLOWED_HOSTS=.*|FLEET_ALLOWED_HOSTS=$WEB_DOMAIN,127.0.0.1,localhost|" \
      -e "s|^# FLEET_CSRF_TRUSTED_ORIGINS=.*|FLEET_CSRF_TRUSTED_ORIGINS=https://$WEB_DOMAIN|" \
      -e "s|^# FLEET_BEHIND_HTTPS_PROXY=.*|FLEET_BEHIND_HTTPS_PROXY=true|" \
      -e "s|^FLEET_PUBLIC_GPS_HOST=.*|FLEET_PUBLIC_GPS_HOST=$GPS_DOMAIN|" \
      -e "/^MSSQL_SA_PASSWORD=/d" \
      "$APP/.env.example" > "$ENVF"
  echo "FLEET_LOG_DIR=/var/log/meltice-gps-tracker" >> "$ENVF"
  if [[ $DB_MODE == sqlite ]]; then
    sed -i -e "s|^FLEET_DB_ENGINE=.*|FLEET_DB_ENGINE=sqlite|" "$ENVF"
    echo "FLEET_SQLITE_PATH=/var/lib/meltice-gps-tracker/fleet.sqlite3" >> "$ENVF"
  elif [[ $DB_MODE == external ]]; then
    sed -i -e "s|^FLEET_DB_HOST=.*|FLEET_DB_HOST=$DB_HOST|" -e "s|^FLEET_DB_PORT=.*|FLEET_DB_PORT=${DB_PORT:-1433}|" \
           -e "s|^FLEET_DB_NAME=.*|FLEET_DB_NAME=$DB_NAME|" -e "s|^FLEET_DB_USER=.*|FLEET_DB_USER=$DB_USER|" \
           -e "s|^FLEET_DB_PASSWORD=.*|FLEET_DB_PASSWORD=$DB_PASSWORD|" "$ENVF"
  else
    APPPW="App-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')!7"
    sed -i -e "s|^FLEET_DB_HOST=.*|FLEET_DB_HOST=127.0.0.1|" -e "s|^FLEET_DB_PASSWORD=.*|FLEET_DB_PASSWORD=$APPPW|" "$ENVF"
  fi
  chown root:fleet "$ENVF"; chmod 640 "$ENVF"
  echo "   Admin user: admin   password: $ADMINPW   (also stored in $ENVF - change it after first login)"
else
  echo "==> Keeping existing $ENVF"
fi

run() { sudo -u fleet bash -c "set -a; source $ENVF; set +a; cd $APP; $VENV/bin/python manage.py $*"; }
# as root (can read the sa password file); logs go to a throwaway folder so no root-owned log files are left behind
run_root() { FLEET_LOG_DIR=$(mktemp -d) bash -c "set -a; source $ENVF; set +a; cd $APP; $VENV/bin/python manage.py $*"; }

if grep -q "^FLEET_DB_ENGINE=mssql" "$ENVF"; then
  echo "==> SQL Server database and application login"
  if [[ $DB_MODE == docker ]]; then
    run_root mssql_setup --admin-password-file "$SECRETS" --wait 120
  elif [[ -n ${DB_ADMIN_PASSWORD:-} ]]; then
    FLEET_DB_ADMIN_USER="${DB_ADMIN_USER:-sa}" FLEET_DB_ADMIN_PASSWORD="$DB_ADMIN_PASSWORD" run_root mssql_setup --wait 60
  else
    echo "   DB_ADMIN_PASSWORD not given - assuming database $DB_NAME and login $DB_USER already exist"
  fi
fi

echo "==> Migrate / static / admin"
run migrate --noinput
run createcachetable
run collectstatic --noinput -v0
run ensure_admin
# self-test IMEI so tools/fmc_probe.py and the web self-test work immediately (disable it later in Devices)
run register_device 356307042441013 Self-test-simulator --model SIMULATOR --group Test
run check

echo "==> systemd units"
install -m 644 "$APP/deploy/hostinger/fleet-receiver.service" /etc/systemd/system/fleet-receiver.service
install -m 644 "$APP/deploy/hostinger/gunicorn-fleet.service" /etc/systemd/system/gunicorn-fleet.service
systemctl daemon-reload
systemctl enable --now gunicorn-fleet fleet-receiver
systemctl restart gunicorn-fleet fleet-receiver

echo "==> Nginx server block for $WEB_DOMAIN"
SITE=/etc/nginx/sites-available/$WEB_DOMAIN
if [[ ! -f $SITE ]]; then
  tar czf "/root/nginx-backup-$(date +%Y%m%d-%H%M%S).tgz" /etc/nginx
  sed "s/fleet.melticetech.com/$WEB_DOMAIN/g" "$APP/deploy/hostinger/nginx-fleet.melticetech.com.conf" > "$SITE"
  ln -sf "$SITE" "/etc/nginx/sites-enabled/$WEB_DOMAIN"
  if nginx -t; then systemctl reload nginx; else echo "!! nginx -t failed - removing new site"; rm -f "/etc/nginx/sites-enabled/$WEB_DOMAIN" "$SITE"; exit 1; fi
else
  echo "   $SITE exists - left unchanged"
fi

echo "==> Firewall (not changed automatically)"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  echo "   UFW is active. To accept tracker traffic run:  sudo ufw allow 5027/tcp"
else
  echo "   UFW inactive. Do NOT enable it before allowing SSH (sudo ufw allow OpenSSH)."
fi
echo "   Also add TCP 5027 (and 80/443) in hPanel -> VPS -> Security -> Firewall if that firewall is enabled."

sleep 3
systemctl --no-pager --lines=3 status fleet-receiver gunicorn-fleet | grep -E "Active|Loaded" || true
cat <<MSG

Done.
  Web (after DNS + TLS):  https://$WEB_DOMAIN      (now: curl -I http://127.0.0.1:9040/login/)
  Tracker endpoint:       $GPS_DOMAIN:5027  (TCP, DNS-only record)
  Next:  sudo certbot --nginx -d $WEB_DOMAIN
         sudo -u fleet bash -c 'cd $APP && $VENV/bin/python tools/fmc_probe.py 127.0.0.1 5027 --quick'
MSG
