FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OCOOPA_DB_PATH=/data/ocoopa_monitor.db

WORKDIR /app

COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
COPY migrations ./migrations

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e ".[api]"

RUN mkdir -p /data

CMD ["python", "-m", "ocoopa_monitor.cli", "scheduler"]
