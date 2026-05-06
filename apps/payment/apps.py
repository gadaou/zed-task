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

    def ready(self) -> None:
        """Auto-register the dummy gateways when the app finishes loading.

        Importing ``apps.payment.gateways.dummy`` triggers the module-level
        ``register_payment_gateway(...)`` calls at the bottom of that module,
        making the following slugs available in the registry without any
        further configuration:

            - ``"dummy_success"``  — always succeeds (DummySuccessGateway)
            - ``"dummy_failing"``  — always declines  (DummyFailingGateway)
            - ``"dummy_timeout"``  — always times out  (DummyTimeoutGateway)
            - ``"mock"``           — alias for "dummy_success"

        Real gateway integrations (Stripe, HyperPay, Tabby) plug in by
        registering themselves the same way — typically in their own app's
        ``ready()`` or in a dedicated ``gateways.py`` module that is imported
        here alongside the dummy module.

        ADR-NOTE: The import is deferred to ``ready()`` (rather than at module
        level in ``apps.py``) to avoid import-time side effects before Django
        models are fully loaded — ``register_payment_gateway`` creates
        gateway instances that may depend on settings.
        """
        import apps.payment.gateways.dummy  # noqa: F401 — side-effect import
