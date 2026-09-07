# Build context is the repository root, so the shared otel_common package can
# be copied in alongside the service.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY services/inventory/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Installed with --no-deps: requirements.txt above is the single source of
# truth for versions, so the shared package cannot drag in a different SDK.
COPY services/common ./common
RUN pip install --no-deps ./common

COPY services/inventory/app ./app

RUN useradd --uid 10001 --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
