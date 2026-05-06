"""Celery application for cart_system.

Configures Celery from Django settings (namespace ``CELERY``), declares the
three named queues used in this system, and auto-discovers task modules in
every installed Django app.

Queue layout (PROJECT_SPEC §4.6):
- ``payments``      — payment authorisation / capture / refund tasks.
                      Slow or unreachable gateways must not starve the others.
- ``invoices``      — invoice generation per successful order (Celery beat +
                      one-shot tasks keyed by order_id).
- ``notifications`` — email / webhook dispatch. Fire-and-forget with retries.

Only the ``payments`` queue is used in this iteration; the other two are
declared here so the deploy manifest and docker-compose can configure
dedicated workers without a later rename-and-deploy cycle.

Usage
-----
Start a worker consuming the payments queue::

    celery -A cart_system worker -Q payments --loglevel=info

Start all three workers::

    celery -A cart_system worker -Q payments,invoices,notifications --loglevel=info

Schedule periodic tasks (Celery beat for idempotency sweep, etc.)::

    celery -A cart_system beat --loglevel=info
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cart_system.settings.dev")

app = Celery("cart_system")

# Read configuration from Django settings under the CELERY_ namespace.
# This means CELERY_BROKER_URL in settings maps to app.conf.broker_url, etc.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Discover task modules automatically in every installed app.
# Each app may define tasks in an ``tasks.py`` module; Celery picks them up
# without manual registration.
app.autodiscover_tasks()
