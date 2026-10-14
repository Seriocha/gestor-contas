FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

ENV GESTOR_DATA_DIR=/data \
    GESTOR_BACKUP_DIR=/backup \
    PORT=8000 \
    TZ=Europe/Lisbon \
    PYTHONUNBUFFERED=1

VOLUME ["/data", "/backup"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/healthz', timeout=3)" || exit 1

CMD ["sh", "-c", "uvicorn --factory app.main:create_app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
