"""
Full backup of the SQL Server database to a .bak file.

The path is as seen by the SQL Server process. With the Docker SQL Server from
deploy/hostinger/install_sqlserver.sh, /var/opt/mssql/backup inside the container is
/var/backups/meltice-gps-tracker/mssql on the VPS.

    python manage.py mssql_backup                         # -> /var/opt/mssql/backup/<db>-<timestamp>.bak
    python manage.py mssql_backup --dir D:\\Backups        # Windows SQL Server
"""
import os
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "BACKUP DATABASE to a .bak file on the SQL Server host."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=os.environ.get("FLEET_DB_BACKUP_DIR", "/var/opt/mssql/backup"))

    def handle(self, *args, **o):
        if connection.vendor != "microsoft":
            raise CommandError("Database is not SQL Server.")
        name = settings.DATABASES["default"]["NAME"]
        sep = "\\" if "\\" in o["dir"] else "/"
        path = f"{o['dir'].rstrip(sep)}{sep}{name}-{datetime.now():%Y%m%d-%H%M%S}.bak"
        sql = f"BACKUP DATABASE [{name}] TO DISK = %s WITH INIT, CHECKSUM, NAME = %s"
        with connection.cursor() as cur:
            cur.execute(sql, [path, f"{name} full backup"])
            while cur.nextset():  # drain informational result sets so the backup completes
                pass
        self.stdout.write(self.style.SUCCESS(f"Backup written to {path} (on the SQL Server host)"))
