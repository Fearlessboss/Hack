FROM python:3.11-slim

# System deps (tgcrypto ke liye build tools chahiye)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        python-telegram-bot==20.7 \
        telethon==1.36.0 \
        pyrogram==2.0.106 \
        tgcrypto \
        pymongo==4.8.0 \
        dnspython==2.6.1

# Bot code copy
COPY . .

# Sessions folder
RUN mkdir -p /app/account_sessions

# Unbuffered logs
ENV PYTHONUNBUFFERED=1

CMD ["python", "sell.py"]
