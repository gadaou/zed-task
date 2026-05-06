"""URL configuration for the core app."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    # Production-style probe endpoints.
    path("health/", views.health, name="health"),
    path("ready/", views.ready, name="ready"),
    # Compatibility aliases retained for existing infra/scripts.
    path("healthz", views.health, name="healthz"),
    path("readyz", views.ready, name="readyz"),
]
