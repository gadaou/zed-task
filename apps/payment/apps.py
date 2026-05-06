"""Payment app — payment methods, intents, and the gateway plug-in system.

Implements PROJECT_SPEC §3.3 (pluggable gateways behind a ``PaymentGateway``
protocol + registry) and §4.6 (Celery-driven async authorization with a
strict ``PaymentIntent`` finite state machine).
"""

from django.apps import AppConfig


class PaymentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payment"
    label = "payment"
    verbose_name = "Payments"
