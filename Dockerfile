FROM python:3.13-alpine

WORKDIR /app

# Install system dependencies
RUN apk add --no-cache curl

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY config.py .
COPY app.py .
COPY templates/ templates/

# Create a non-root user
RUN adduser -D -H wildduck
USER wildduck

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/login || exit 1

CMD ["python", "app.py"]
