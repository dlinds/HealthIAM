VENV ?= .venv
PY := $(VENV)/bin/python
RUFF := $(VENV)/bin/ruff

.PHONY: install run migrate makemigrations seed roles test lint fmt up down logs shell

install:
	uv venv $(VENV) --python 3.11 || python3 -m venv $(VENV)
	uv pip install --python $(PY) -r pyproject.toml --extra dev

run:
	$(PY) manage.py runserver 0.0.0.0:8000

migrate:
	$(PY) manage.py migrate

makemigrations:
	$(PY) manage.py makemigrations

roles:
	$(PY) manage.py bootstrap_roles

seed: migrate roles
	$(PY) manage.py seed_demo

test:
	$(PY) -m pytest

lint:
	$(RUFF) check .
	$(RUFF) format --check .

fmt:
	$(RUFF) check --fix .
	$(RUFF) format .

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f web

shell:
	$(PY) manage.py shell
