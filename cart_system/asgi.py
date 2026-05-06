"""ASGI entrypoint for cart_system.

Used by async-capable servers (uvicorn, daphne, hypercorn). Defaults to the
``prod`` settings; override with ``DJANGO_SETTINGS_MODULE`` if needed.

See: https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cart_system.settings.prod")

application = get_asgi_application()
