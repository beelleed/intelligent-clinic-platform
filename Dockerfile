FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install --requirement requirements.txt


FROM python:3.13-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_RELOAD=false \
    PORT=8080 \
    MCP_SERVERS_CONFIG=/app/mcp_config.json \
    CLINIC_DB_PATH=/app/data/clinic_operations.db \
    CLINIC_TIMEZONE=America/Los_Angeles \
    CLINIC_APPOINTMENTS_DB_PATH=/app/data/clinic_appointments.db \
    CLINIC_KNOWLEDGE_DIR=/app/clinic_knowledge

RUN groupadd --system appgroup && \
    useradd --system --gid appgroup --home-dir /app appuser && \
    mkdir --parents /app/data && \
    chown --recursive appuser:appgroup /app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appgroup main.py mcp_config.json ./
COPY --chown=appuser:appgroup app ./app
COPY --chown=appuser:appgroup mcp_servers ./mcp_servers
COPY --chown=appuser:appgroup clinic_knowledge ./clinic_knowledge
COPY --chown=appuser:appgroup static ./static
COPY --chown=appuser:appgroup data/clinic_faq.txt ./data/clinic_faq.txt

USER appuser

EXPOSE 8080

CMD ["python", "main.py"]
