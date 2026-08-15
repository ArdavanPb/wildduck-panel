FROM python:3.13-alpine

WORKDIR /app

# Install system dependencies (build deps for brotli/zstandard; curl for healthcheck)
RUN apk add --no-cache curl && \
    apk add --no-cache --virtual .build-deps gcc musl-dev libffi-dev

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    apk del .build-deps

# Copy application code
COPY config.py .
COPY app.py .
COPY templates/ templates/

# Create a non-root user and ensure /app is writable
RUN adduser -D -H wildduck && \
    mkdir -p /app/instance && \
    chown -R wildduck:wildduck /app
USER wildduck

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

CMD ["python", "app.py"]
