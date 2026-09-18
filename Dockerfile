FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

COPY . .
# SECRET_KEY and AUTH_LOCAL_LOGIN are build-only: prod settings refuse to import
# without a secret key and at least one auth backend. Neither value is baked into
# the image; both come from the environment at runtime.
RUN SECRET_KEY=build-only AUTH_LOCAL_LOGIN=true python manage.py collectstatic --noinput

# Fixed uid 568, matching the TrueNAS apps user, so a bind-mounted media
# dataset can be owned by an account that exists on the host too. A bind mount
# does no uid mapping, so this uid is what reaches the dataset.
RUN useradd --create-home --uid 568 app && chown -R app:app /app
USER app

EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
# --timeout 120: "Sync now" under Admin > Active Directory reads the whole directory inside
# one request; gunicorn's 30 s default would kill the worker mid-read on a large domain.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "--access-logfile", "-"]
