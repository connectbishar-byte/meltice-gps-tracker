#!/usr/bin/env bash
# Microsoft SQL Server 2022 (Express edition, free for production) on the Hostinger VPS, in Docker.
#   sudo bash deploy/hostinger/install_sqlserver.sh
# Called automatically by install.sh when DB_MODE=docker (the default). Idempotent.
#
#  - needs ~2 GB RAM free (Hostinger KVM 1 = 4 GB is enough for this app)
#  - listens on 127.0.0.1:1433 ONLY (never exposed to the internet)
#  - data:    /var/opt/meltice-mssql/data
#  - backups: /var/backups/meltice-gps-tracker/mssql   (= /var/opt/mssql/backup inside the container)
#  - sa password: /etc/meltice-gps-tracker/mssql.env (root only)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
NAME=meltice-mssql
IMAGE=${MSSQL_IMAGE:-mcr.microsoft.com/mssql/server:2022-latest}
EDITION=${MSSQL_PID:-Express}
SECRETS=/etc/meltice-gps-tracker/mssql.env
DATA=/var/opt/meltice-mssql
BACKUP=/var/backups/meltice-gps-tracker/mssql

mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if (( mem_mb < 1900 )); then
  echo "!! SQL Server needs at least 2 GB RAM; this VPS has ${mem_mb} MB. Use a bigger plan or an external SQL Server (DB_MODE=external)."
  exit 1
fi

if ! command -v docker >/dev/null; then
  echo "==> Installing Docker (Ubuntu docker.io package)"
  apt-get update -qq && apt-get install -y -qq docker.io >/dev/null
  systemctl enable --now docker
fi

install -d -o root -g root -m 750 /etc/meltice-gps-tracker
if [[ ! -f $SECRETS ]]; then
  # SQL Server policy: 8+ chars from 3 of 4 classes. token_urlsafe gives upper/lower/digits; add symbols.
  PW="Sa-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')!9"
  printf 'MSSQL_SA_PASSWORD=%s\n' "$PW" > "$SECRETS"
  chmod 600 "$SECRETS"
  echo "==> Generated sa password in $SECRETS"
fi
set -a; source "$SECRETS"; set +a

install -d -m 770 "$DATA" "$BACKUP"
chown -R 10001:0 "$DATA" "$BACKUP"     # mssql user inside the container

if docker inspect "$NAME" >/dev/null 2>&1; then
  echo "==> Container $NAME exists - starting it"
  docker start "$NAME" >/dev/null
else
  echo "==> Starting SQL Server ($EDITION) container $NAME on 127.0.0.1:1433"
  docker run -d --name "$NAME" --restart unless-stopped \
    -e ACCEPT_EULA=Y -e "MSSQL_PID=$EDITION" -e MSSQL_SA_PASSWORD \
    -e MSSQL_MEMORY_LIMIT_MB="${MSSQL_MEMORY_LIMIT_MB:-1536}" \
    -p 127.0.0.1:1433:1433 \
    -v "$DATA:/var/opt/mssql" -v "$BACKUP:/var/opt/mssql/backup" \
    "$IMAGE" >/dev/null
fi

echo -n "==> Waiting for SQL Server to accept logins"
for i in $(seq 1 60); do
  if docker exec "$NAME" /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -Q "SELECT 1" -b -o /dev/null 2>/dev/null; then
    echo " - ready"; break
  fi
  echo -n "."; sleep 3
  [[ $i -eq 60 ]] && { echo; echo "!! SQL Server did not start. See: docker logs $NAME"; exit 1; }
done
docker exec "$NAME" /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -h -1 \
  -Q "SET NOCOUNT ON; SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(20)) + ' ' + CAST(SERVERPROPERTY('Edition') AS varchar(60))"
echo "SQL Server is listening on 127.0.0.1:1433 (not reachable from the internet)."
