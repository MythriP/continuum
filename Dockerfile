# Continuum: reconcile reference application.
#   docker build -t continuum-reconcile .
#   docker run --rm --env-file .env -v reconcile-state:/app/var continuum-reconcile work

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CONTINUUM_HOME=/app

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY data ./data

RUN useradd --create-home --uid 1000 continuum \
    && mkdir -p /app/var \
    && chown continuum:continuum /app/var
USER continuum

# case store, ADK sessions and replay cache
VOLUME ["/app/var"]

ENTRYPOINT ["reconcile"]
CMD ["--help"]
