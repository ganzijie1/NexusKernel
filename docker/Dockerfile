ARG NEXUS_KERNEL_RUNTIME_EXTRAS=ai

FROM python:3.11-slim AS builder

ARG NEXUS_KERNEL_RUNTIME_EXTRAS

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /build
COPY pyproject.toml README.md ./
COPY nexus_kernel ./nexus_kernel
COPY app ./app
RUN NEXUS_KERNEL_RUNTIME_EXTRAS="${NEXUS_KERNEL_RUNTIME_EXTRAS}" python -c \
        'import os; allowed={"ai","mongo","redis","qdrant","otel","postgres"}; tokens=os.environ["NEXUS_KERNEL_RUNTIME_EXTRAS"].split(","); requested=set(tokens); raise SystemExit(0 if requested and len(tokens)==len(requested) and requested.issubset(allowed) else "invalid NEXUS_KERNEL_RUNTIME_EXTRAS")' \
    && python -m pip wheel --wheel-dir /wheels ".[${NEXUS_KERNEL_RUNTIME_EXTRAS}]"

FROM python:3.11-slim AS runtime

ARG NEXUS_KERNEL_RUNTIME_EXTRAS

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 nexus_kernel \
    && useradd --uid 10001 --gid nexus_kernel --create-home nexus_kernel \
    && python -m venv /opt/venv
COPY --from=builder /wheels /wheels
RUN /opt/venv/bin/pip install --no-cache-dir --no-index --find-links=/wheels "nexus-kernel-starter[${NEXUS_KERNEL_RUNTIME_EXTRAS}]" \
    && rm -rf /wheels

WORKDIR /app
COPY --chown=nexus_kernel:nexus_kernel config ./config
USER 10001:10001
EXPOSE 8030
CMD ["python", "-m", "app.launcher"]
