FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/hades.db HADES_CONFIG=/app/config.yaml

WORKDIR /app/engine
COPY engine/pyproject.toml ./
COPY engine/hades ./hades
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 hades \
    && mkdir -p /data && chown hades /data

USER hades
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "-m", "hades", "run", "--host", "0.0.0.0", "--port", "8000"]
