FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SITE_ROOT=/srv/wedding-site/current \
    RSVP_DB_PATH=/data/rsvp.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple -r requirements.txt \
    && groupadd --gid 10001 wedding \
    && useradd --uid 10001 --gid wedding --no-create-home --shell /usr/sbin/nologin wedding \
    && mkdir -m 0700 /data \
    && chown wedding:wedding /data

COPY app.py .
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "2", "--access-logfile", "-", "--error-logfile", "-", "--no-control-socket", "app:create_app()"]
