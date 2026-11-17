ARG PYTHON_IMAGE=python:3.13-slim@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ADMIN_HOST=0.0.0.0 \
    ADMIN_PORT=8000 \
    DB_PATH=/app/data/app.db \
    KB_DIR=/app/data/kb

WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 10001 assistant \
    && useradd --uid 10001 --gid assistant --no-create-home assistant \
    && mkdir -p /app/data \
    && chown assistant:assistant /app/data

COPY app ./app
# Сертификат Минцифры нужен MAX и опционально GigaChat: сборка падает, если .dockerignore его потерял.
RUN test -f /app/app/certs/russian_trusted_root_ca.pem
COPY eval/*.py eval/questions*.csv ./eval/
COPY tools/*.py ./tools/
COPY deploy/container/entrypoint.py ./deploy/container/entrypoint.py
COPY data/kb ./kb-seed

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
ENTRYPOINT ["python", "/app/deploy/container/entrypoint.py"]
CMD ["python", "-m", "app"]
