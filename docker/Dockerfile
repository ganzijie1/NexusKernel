FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY nexuskernel ./nexuskernel
COPY app ./app
RUN python -m pip wheel --wheel-dir /wheels ".[ai]"

FROM python:3.11-slim AS runtime

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 nexuskernel \
    && useradd --uid 10001 --gid nexuskernel --create-home nexuskernel \
    && python -m venv /opt/venv
COPY --from=builder /wheels /wheels
RUN /opt/venv/bin/pip install --no-cache-dir --no-index --find-links=/wheels "nexuskernel-starter[ai]" \
    && rm -rf /wheels

WORKDIR /app
COPY --chown=nexuskernel:nexuskernel config ./config
USER 10001:10001
EXPOSE 8030
CMD ["python", "-m", "app.launcher"]
