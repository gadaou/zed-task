"""cart_system Django package.

Imports the Celery application on Django startup so that tasks registered
with ``@shared_task`` are always associated with this app instance — a
pattern required by the Celery + Django integration guide.

See: https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html
"""

from __future__ import annotations

from cart_system.celery import app as celery_app

__all__ = ["celery_app"]
