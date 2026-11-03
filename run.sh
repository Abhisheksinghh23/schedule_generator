#!/bin/sh
cd "$(dirname "$0")" && pip install -r requirements.txt -q && \
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin123}" uvicorn app:app --host 0.0.0.0 --port 8000
