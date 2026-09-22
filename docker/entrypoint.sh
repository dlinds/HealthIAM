#!/bin/sh
set -e
python manage.py migrate --noinput
python manage.py bootstrap_roles
python manage.py bootstrap_person_types
exec "$@"
