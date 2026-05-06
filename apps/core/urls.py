"""URL configuration for the core app."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
]
