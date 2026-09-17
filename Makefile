VENV ?= .venv
PY := $(VENV)/bin/python
RUFF := $(VENV)/bin/ruff

.PHONY: install run migrate makemigrations seed roles test lint fmt up down logs shell release

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

# Tag and push a release; GitHub Actions builds and pushes the image to GHCR.
# Usage: make release VERSION=0.2.0 (a leading v is accepted too)
release:
	@echo "$(VERSION)" | grep -Eq '^v?[0-9]+\.[0-9]+\.[0-9]+$$' || \
		{ echo "usage: make release VERSION=1.2.3"; exit 1; }
	@test -z "$$(git status --porcelain)" || { echo "working tree is dirty"; exit 1; }
	git tag -a $(VERSION) -m "HealthIAM $(VERSION)"
	git push origin $(VERSION)
	@echo "Pushed $(VERSION). Watch Actions, then set the TrueNAS app image tag to $(VERSION:v%=%)."
