PYTHON := .venv/bin/python

.PHONY: check lint test audit install-dev lock

check: lint test

lint:
	$(PYTHON) -m ruff check app eval tools tests

test:
	$(PYTHON) -m pytest -q --cov --cov-report=term-missing

audit:
	$(PYTHON) -m pip_audit -r requirements-dev.lock --require-hashes --disable-pip --progress-spinner off

install-dev:
	uv pip install --python $(PYTHON) --require-hashes -r requirements-dev.lock

lock:
	uv pip compile requirements.txt --universal --python-version 3.11 --generate-hashes --custom-compile-command 'make lock' -o requirements.lock --quiet
	uv pip compile requirements-dev.txt -c requirements.lock --universal --python-version 3.11 --generate-hashes --custom-compile-command 'make lock' -o requirements-dev.lock --quiet
