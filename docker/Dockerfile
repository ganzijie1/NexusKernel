ARG NEXUS_KERNEL_RUNTIME_EXTRAS=ai

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS builder

ARG NEXUS_KERNEL_RUNTIME_EXTRAS

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /build
COPY pyproject.toml README.md ./
COPY constraints/py311-313.txt ./constraints/py311-313.txt
COPY nexus_kernel ./nexus_kernel
COPY app ./app
RUN --mount=type=secret,id=pip_config,target=/etc/pip.conf,required=false \
    NEXUS_KERNEL_RUNTIME_EXTRAS="${NEXUS_KERNEL_RUNTIME_EXTRAS}" python -c \
        'import os; allowed={"ai","mongo","redis","qdrant","otel","postgres"}; tokens=os.environ["NEXUS_KERNEL_RUNTIME_EXTRAS"].split(","); requested=set(tokens); raise SystemExit(0 if requested and len(tokens)==len(requested) and requested.issubset(allowed) else "invalid NEXUS_KERNEL_RUNTIME_EXTRAS")' \
    && python -m pip wheel --timeout 120 --retries 8 \
        --constraint constraints/py311-313.txt \
        --wheel-dir /wheels ".[${NEXUS_KERNEL_RUNTIME_EXTRAS}]"

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS runtime

ARG NEXUS_KERNEL_RUNTIME_EXTRAS

ENV PATH=/opt/venv/bin:$PATH \
    ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false \
    LITELLM_LOCAL_MODEL_COST_MAP=true \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT \
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
