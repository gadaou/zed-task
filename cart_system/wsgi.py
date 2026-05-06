"""WSGI entrypoint for cart_system.

Production servers (gunicorn, uwsgi) load this module. Defaults to the
``prod`` settings; override with ``DJANGO_SETTINGS_MODULE`` if needed.

See: https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cart_system.settings.prod")

application = get_wsgi_application()
