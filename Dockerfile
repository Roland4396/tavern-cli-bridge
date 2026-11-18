FROM python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 1000 runner && useradd --uid 1000 --gid 1000 --create-home --shell /bin/sh runner

FROM base AS gateway
COPY --chmod=644 deploy/gateway.py /app/gateway.py
RUN chmod 755 /app
USER 1000:1000
ENTRYPOINT ["python3", "/app/gateway.py"]

FROM base AS seed
WORKDIR /fixture
COPY tools/build_fixture.py tools/fixture-schema.sql ./tools/
COPY src/agy_bridge/__init__.py src/agy_bridge/protocol.py ./src/agy_bridge/
RUN python3 tools/build_fixture.py

FROM base AS app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates util-linux iproute2 procps \
    && rm -r /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements.lock
COPY --chown=0:0 vendor/antigravity /opt/antigravity/antigravity
RUN chmod 0555 /opt/antigravity/antigravity
COPY --chown=0:0 src/agy_bridge /app/agy_bridge
COPY --from=seed /fixture/src/agy_bridge/fixtures/session.db /app/agy_bridge/fixtures/session.db
COPY --chown=0:0 --chmod=644 deploy/healthcheck.py /healthcheck.py
RUN chmod -R a+rX /app/agy_bridge && python3 -m compileall -q /app/agy_bridge
ENV PYTHONPATH=/app AGY_CLI_BINARY=/opt/antigravity/antigravity AGY_CLI_PROC_MOUNT=0 \
    AGY_CLI_DISABLE_AUTO_UPDATE=true AGY_ACCOUNTS_ROOT=/accounts HOME=/home/runner
WORKDIR /app
USER 1000:1000
ENTRYPOINT ["python3", "-m", "agy_bridge.hybrid"]
CMD ["--config", "/config/bridge.json"]
