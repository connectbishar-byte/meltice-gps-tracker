#!/bin/sh
set -e
wait_for_schema() {
  # the web container creates the database and applies migrations; other roles wait for it
  i=0; until python manage.py migrate --check >/dev/null 2>&1 || [ $i -ge 60 ]; do i=$((i+1)); sleep 3; done
}
case "$1" in
  web)
    export FLEET_PROCESS=web
    if [ "${FLEET_DB_ENGINE:-mssql}" = "mssql" ] && [ -n "${FLEET_DB_ADMIN_PASSWORD:-}" ]; then
      python manage.py mssql_setup --wait 120     # create database + app login on first start
    fi
    python manage.py migrate --noinput
    python manage.py createcachetable
    python manage.py ensure_admin
    if [ "${FLEET_SEED_DEMO:-false}" = "true" ]; then python manage.py seed_demo; fi
    exec gunicorn fleet_project.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-3} --access-logfile -
    ;;
  receiver)
    export FLEET_PROCESS=receiver
    wait_for_schema
    exec python manage.py run_receiver --host 0.0.0.0 --port 5027
    ;;
  sim)
    export FLEET_PROCESS=simulator
    shift
    wait_for_schema
    python manage.py seed_demo --devices-only
    exec python manage.py simulate_device "$@"
    ;;
  *)
    exec python manage.py "$@"
    ;;
esac
