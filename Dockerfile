# =====================================================
#  ZUDO Account Seller Bot — Dockerfile
# =====================================================
FROM python:3.11-slim

# Faster, cleaner Python output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps (tgcrypto needs gcc; ca-certificates for TLS)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        build-essential \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source
COPY . .

# Persisted folders (mount as volumes in production)
RUN mkdir -p /app/account_sessions /app/sessions
VOLUME ["/app/account_sessions", "/app/sessions"]

# bot_data.json is created at runtime; keep writable
ENV TZ=Asia/Kolkata

CMD ["python", "sell.py"]
