FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first so this layer is cached until pyproject.toml changes.
COPY pyproject.toml README.md ./
RUN mkdir app && touch app/__init__.py \
    && pip install ".[bench]" && pip uninstall -y nordcart-sync-engine

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY benchmarks ./benchmarks

# Run as an unprivileged user.
RUN useradd --system --uid 10001 appuser
USER appuser

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
