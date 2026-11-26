#!/usr/bin/env bash
# Nightly backup: SQL Server .bak (or SQLite file) + env file, 14-day retention.
# Root crontab:  15 2 * * * bash /srv/meltice-gps-tracker/app/deploy/hostinger/backup.sh >> /var/log/meltice-gps-tracker/backup.log 2>&1
set -euo pipefail
APP=/srv/meltice-gps-tracker/app; VENV=/srv/meltice-gps-tracker/venv; ENVF=/etc/meltice-gps-tracker/fleet.env
OUT=/var/backups/meltice-gps-tracker; install -d -m 700 "$OUT"
TS=$(date +%Y%m%d-%H%M%S)
ENGINE=$(grep -E "^FLEET_DB_ENGINE=" "$ENVF" | cut -d= -f2 || true)

if [[ ${ENGINE:-mssql} == mssql ]]; then
  # Written by SQL Server itself. With the Docker SQL Server (install_sqlserver.sh) the container path
  # /var/opt/mssql/backup is $OUT/mssql on this VPS. For an external SQL Server set FLEET_DB_BACKUP_DIR in fleet.env
  # to a folder on that server.
  sudo -u fleet bash -c "set -a; source $ENVF; set +a; cd $APP; $VENV/bin/python manage.py mssql_backup"
  [[ -d $OUT/mssql ]] && find "$OUT/mssql" -name "*.bak" -mtime +14 -delete
else
  DB=$(grep -E "^FLEET_SQLITE_PATH=" "$ENVF" | cut -d= -f2 || true)
  DB=${DB:-/var/lib/meltice-gps-tracker/fleet.sqlite3}
  [[ -f $DB ]] && sqlite3 "$DB" ".backup '$OUT/fleet-$TS.sqlite3'" && gzip "$OUT/fleet-$TS.sqlite3"
fi
cp "$ENVF" "$OUT/fleet-$TS.env"; chmod 600 "$OUT/fleet-$TS.env"
find "$OUT" -maxdepth 1 -type f -mtime +14 -delete
echo "$(date -Is) backup done ($ENGINE)"
