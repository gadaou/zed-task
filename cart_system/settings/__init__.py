"""Settings package for cart_system.

Pick the concrete environment via the ``DJANGO_SETTINGS_MODULE`` env var:

* ``cart_system.settings.dev``   — local development (default for ``manage.py``)
* ``cart_system.settings.prod``  — production (default for ``asgi``/``wsgi``)
* ``cart_system.settings.test``  — test runner (set by pytest config)

Never import from ``cart_system.settings.base`` directly outside this package.
"""
