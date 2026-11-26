# Ubuntu 24.04 + Python 3.12 + Microsoft ODBC Driver 18 for SQL Server (same stack as the Hostinger VPS install)
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FLEET_LOG_DIR=/data/logs PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv curl gnupg ca-certificates \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && apt-get purge -y gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -r -u 1001 fleet && mkdir -p /data/logs && chown -R fleet /data /app \
    && FLEET_DB_ENGINE=sqlite FLEET_SQLITE_PATH=/tmp/build.sqlite3 FLEET_LOG_DIR=/tmp/buildlogs \
       python manage.py collectstatic --noinput -v0
USER fleet
ENV HOME=/tmp
EXPOSE 8000 5027
COPY deploy/docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["sh", "/entrypoint.sh"]
CMD ["web"]
