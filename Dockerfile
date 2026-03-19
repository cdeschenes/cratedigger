FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create non-root user matching workspace PUID=1002/PGID=990 convention,
# and ensure the /data volume mount point is writable by that user.
RUN groupadd -g 990 appgroup \
    && useradd -u 1002 -g 990 -s /bin/sh -M appuser \
    && mkdir -p /data \
    && chown -R appuser:appgroup /data /app

USER appuser

EXPOSE 8080

CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8080"]
