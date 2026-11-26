"""
Create the SQL Server database and a least-privilege application login, idempotently.

Connects to `master` with an administrator login (default `sa`) and then:
  * CREATE DATABASE <FLEET_DB_NAME>                 (if missing)
  * CREATE LOGIN <FLEET_DB_USER> WITH PASSWORD ...  (if missing; password updated with --reset-password)
  * CREATE USER + db_owner on that database only    (needed for migrations; no server-level rights)

    FLEET_DB_ADMIN_PASSWORD=... python manage.py mssql_setup
    python manage.py mssql_setup --admin-user sa --admin-password-file /etc/meltice-gps-tracker/mssql.env --wait 120

The application itself only ever uses FLEET_DB_USER / FLEET_DB_PASSWORD.
"""
import os
import re
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _lit(s: str) -> str:
    return "N'" + s.replace("'", "''") + "'"


def _read_password_file(path: str) -> str:
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(("MSSQL_SA_PASSWORD=", "FLEET_DB_ADMIN_PASSWORD=")):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise CommandError(f"No MSSQL_SA_PASSWORD= or FLEET_DB_ADMIN_PASSWORD= line in {path}")


class Command(BaseCommand):
    help = "Create the SQL Server database and application login (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--admin-user", default=os.environ.get("FLEET_DB_ADMIN_USER", "sa"))
        parser.add_argument("--admin-password", default=os.environ.get("FLEET_DB_ADMIN_PASSWORD", ""))
        parser.add_argument("--admin-password-file", default="", help="env-style file with MSSQL_SA_PASSWORD=")
        parser.add_argument("--wait", type=int, default=0, help="Seconds to wait for SQL Server to accept logins")
        parser.add_argument("--reset-password", action="store_true", help="Set the app login password to FLEET_DB_PASSWORD")

    def handle(self, *args, **o):
        import pyodbc

        db = settings.DATABASES["default"]
        if db["ENGINE"] != "mssql":
            raise CommandError("FLEET_DB_ENGINE is not mssql - nothing to do.")
        name, user, pw = db["NAME"], db["USER"], db["PASSWORD"]
        admin_pw = o["admin_password"] or (_read_password_file(o["admin_password_file"]) if o["admin_password_file"] else "")
        if not admin_pw:
            raise CommandError("Give the administrator password: FLEET_DB_ADMIN_PASSWORD, --admin-password or --admin-password-file")
        for label, value in (("FLEET_DB_NAME", name), ("FLEET_DB_USER", user), ("admin user", o["admin_user"])):
            if not NAME_RE.match(value or ""):
                raise CommandError(f"{label} {value!r} must be letters, digits and underscores")
        opts = db.get("OPTIONS", {})
        cs = (f"DRIVER={{{opts.get('driver', 'ODBC Driver 18 for SQL Server')}}};SERVER={db['HOST']},{db['PORT'] or 1433};"
              f"DATABASE=master;UID={o['admin_user']};PWD={{{admin_pw.replace('}', '}}')}}};{opts.get('extra_params', '')}")

        deadline = time.time() + o["wait"]
        while True:
            try:
                cn = pyodbc.connect(cs, autocommit=True, timeout=5)
                break
            except pyodbc.Error as exc:
                if time.time() >= deadline:
                    raise CommandError(f"Cannot connect to SQL Server at {db['HOST']}:{db['PORT']} as {o['admin_user']}: {exc}")
                time.sleep(3)
        cur = cn.cursor()
        version = cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)), "
                              "CAST(SERVERPROPERTY('Edition') AS nvarchar(128))").fetchone()
        self.stdout.write(f"SQL Server {version[0]} ({version[1]}) at {db['HOST']}:{db['PORT']}")

        if cur.execute("SELECT DB_ID(?)", name).fetchone()[0] is None:
            cur.execute(f"CREATE DATABASE [{name}]")
            self.stdout.write(self.style.SUCCESS(f"Created database {name}"))
        else:
            self.stdout.write(f"Database {name} exists")
        # Read-committed snapshot: readers (web UI) never block the receiver's writes and vice versa.
        cur.execute(f"IF (SELECT is_read_committed_snapshot_on FROM sys.databases WHERE name = ?) = 0 "
                    f"ALTER DATABASE [{name}] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE", name)

        if user.lower() == o["admin_user"].lower():
            self.stdout.write(self.style.WARNING("App user is the administrator login; skipping login creation. "
                                                 "Use a dedicated FLEET_DB_USER in production."))
            return
        if not pw:
            raise CommandError("FLEET_DB_PASSWORD is empty")
        exists = cur.execute("SELECT 1 FROM sys.server_principals WHERE name = ?", user).fetchone()
        if not exists:
            cur.execute(f"CREATE LOGIN [{user}] WITH PASSWORD = {_lit(pw)}, DEFAULT_DATABASE = [{name}], CHECK_POLICY = ON")
            self.stdout.write(self.style.SUCCESS(f"Created login {user}"))
        elif o["reset_password"]:
            cur.execute(f"ALTER LOGIN [{user}] WITH PASSWORD = {_lit(pw)}")
            self.stdout.write(f"Password reset for login {user}")
        else:
            self.stdout.write(f"Login {user} exists")
        cur.execute(f"USE [{name}]")
        if not cur.execute("SELECT 1 FROM sys.database_principals WHERE name = ?", user).fetchone():
            cur.execute(f"CREATE USER [{user}] FOR LOGIN [{user}]")
        cur.execute(f"ALTER ROLE db_owner ADD MEMBER [{user}]")
        self.stdout.write(self.style.SUCCESS(f"Login {user} is db_owner of {name} (no server-level permissions)"))
