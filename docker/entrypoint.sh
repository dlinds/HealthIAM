#!/bin/sh
set -e
python manage.py migrate --noinput
python manage.py bootstrap_roles
exec "$@"
