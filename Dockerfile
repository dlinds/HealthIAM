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

# Fixed uid so bind-mounted media datasets can be chowned to a known owner.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-"]
