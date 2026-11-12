# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- build stage
# The compilers live here only. They are not copied into the final image.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libffi-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-prod.txt /tmp/requirements-prod.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements-prod.txt


# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080 \
    LOG_LEVEL=INFO

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY app ./app

# A non-root user: required by most AWS image scanners, and ECS/App Runner are happy with it.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8080

# ECS reads this. App Runner uses its own health check setting (point it at /health).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,sys,urllib.request; url='http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health'; sys.exit(0 if urllib.request.urlopen(url, timeout=4).status==200 else 1)"

# --proxy-headers and --forwarded-allow-ips matter behind an ALB or App Runner:
# without them every request looks like it came from the load balancer.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
