# --------------------------------------------------------------------------- #
#  cart_system — Dockerfile                                                    #
#                                                                              #
#  Two build targets:                                                          #
#    dev  — thin layer on top of base; mounts source as a volume in compose.   #
#    prod — copies source, collects static files, runs gunicorn.               #
#                                                                              #
#  Both targets share the same pip-install layer so Docker's layer cache is   #
#  reused on code-only changes.                                                #
# --------------------------------------------------------------------------- #

ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS base

# --- System dependencies ---------------------------------------------------
# libpq-dev / gcc are needed to build psycopg's C extension fallback.
# They are removed in the prod stage to keep the image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# --- Python environment ----------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (cached layer — only re-runs on requirements.txt change)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# --------------------------------------------------------------------------- #
#  dev target — used by docker compose for local development                  #
# --------------------------------------------------------------------------- #
FROM base AS dev

# Source is mounted as a volume in compose — no COPY needed.
# Entrypoint runs migrations then starts the dev server.
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# --------------------------------------------------------------------------- #
#  prod target — copies source, collects statics, runs gunicorn                #
# --------------------------------------------------------------------------- #
FROM base AS prod

# Copy application source
COPY . .

# Collect static files (silences Django's staticfiles check at startup)
RUN DJANGO_SETTINGS_MODULE=cart_system.settings.prod \
    DJANGO_SECRET_KEY=placeholder-for-collectstatic \
    DJANGO_ALLOWED_HOSTS=* \
    DATABASE_URL=postgres://x:x@localhost/x \
    python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["gunicorn", "cart_system.wsgi:application", \
     "--workers", "4", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
