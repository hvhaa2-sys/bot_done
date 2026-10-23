PYTHON := .venv/bin/python

.PHONY: check lint test audit security install-dev lock

check: lint test

lint:
	$(PYTHON) -m ruff check app eval tools tests

test:
	$(PYTHON) -m pytest -q --cov --cov-report=term-missing

audit:
	$(PYTHON) -m pip_audit -r requirements-dev.lock --require-hashes --disable-pip --progress-spinner off

install-dev:
	uv pip install --python $(PYTHON) --require-hashes -r requirements-dev.lock

# Безопасность: утечки ключей в истории git (gitleaks), опасные места в коде (Bandit, Semgrep: OWASP Top 10,
# Python, секреты) и известные уязвимости зависимостей (audit). Нужны gitleaks (brew install gitleaks) и uv.
# Разобранные ложные срабатывания помечены в коде «# nosec …» / «# nosemgrep: …» с причиной.
security: audit
	gitleaks git . --redact --no-banner
	uvx --quiet bandit==1.8.6 -r app tools eval -q -ll
	uvx --quiet --with pydantic==2.11.7 semgrep==1.140.0 scan --metrics=off --error --quiet \
		--config p/python --config p/owasp-top-ten --config p/secrets \
		app tools eval deploy setup Dockerfile docker-compose.yml

lock:
	uv pip compile requirements.txt --universal --python-version 3.11 --generate-hashes --custom-compile-command 'make lock' -o requirements.lock --quiet
	uv pip compile requirements-dev.txt -c requirements.lock --universal --python-version 3.11 --generate-hashes --custom-compile-command 'make lock' -o requirements-dev.lock --quiet
