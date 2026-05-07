"""Tests for TenantMiddleware.

Covers:
- Resolves tenant from X-Tenant-Domain header → request.tenant populated
- Populates the ContextVar (so managers work inside the view)
- Returns 400 when header is missing on a tenant-required path
- Returns 404 for an unknown domain
- Returns 403 for a disabled tenant
- Exempt paths bypass resolution entirely (no error, request.tenant is None)
- ContextVar is reset after the response — a second request without header
  on a non-exempt path returns 400 (does not see the prior tenant)
"""

from __future__ import annotations

from django.test import RequestFactory, TestCase, override_settings
from django.http import HttpRequest, HttpResponse

from apps.tenant.context import get_current_tenant
from apps.tenant.middleware import TenantMiddleware
from apps.tenant.models import Tenant


def _make_tenant(domain: str, active: bool = True) -> Tenant:
    return Tenant.objects.create(name=domain, domain=domain, is_active=active)


def _simple_view(request: HttpRequest) -> HttpResponse:
    """Minimal view used as the ``get_response`` callable in tests."""
    tenant = getattr(request, "tenant", None)
    ctx_tenant = get_current_tenant()
    body = f"{tenant.domain if tenant else 'none'}|{ctx_tenant.domain if ctx_tenant else 'none'}"
    return HttpResponse(body, status=200)


class TenantMiddlewareResolutionTests(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.middleware = TenantMiddleware(_simple_view)
        self.tenant = _make_tenant("acme.example.com")

    def _request(
        self, path: str = "/api/v1/carts/", domain: str | None = None
    ) -> HttpRequest:
        req = self.factory.get(path)
        if domain is not None:
            req.META["HTTP_X_TENANT_DOMAIN"] = domain
        return req

    def test_valid_header_sets_request_tenant(self) -> None:
        req = self._request(domain="acme.example.com")
        response = self.middleware(req)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(req.tenant, self.tenant)

    def test_valid_header_sets_context_var(self) -> None:
        captured: list[Tenant | None] = []

        def capturing_view(request: HttpRequest) -> HttpResponse:
            captured.append(get_current_tenant())
            return HttpResponse("ok")

        mw = TenantMiddleware(capturing_view)
        req = self._request(domain="acme.example.com")
        mw(req)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0], self.tenant)

    def test_missing_header_returns_400(self) -> None:
        req = self._request()  # no domain header
        response = self.middleware(req)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        import json

        body = json.loads(response.content)
        self.assertIn("tenant/missing-header", body["type"])
        self.assertTrue(body["type"].startswith("https://"))
        self.assertIn("title", body)
        self.assertEqual(body["status"], 400)

    def test_unknown_domain_returns_404(self) -> None:
        req = self._request(domain="ghost.example.com")
        response = self.middleware(req)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        import json

        body = json.loads(response.content)
        self.assertIn("tenant/not-found", body["type"])
        self.assertTrue(body["type"].startswith("https://"))
        self.assertIn("title", body)
        self.assertEqual(body["status"], 404)

    def test_disabled_tenant_returns_403(self) -> None:
        _make_tenant("disabled.example.com", active=False)
        req = self._request(domain="disabled.example.com")
        response = self.middleware(req)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        import json

        body = json.loads(response.content)
        self.assertIn("tenant/disabled", body["type"])
        self.assertTrue(body["type"].startswith("https://"))
        self.assertIn("title", body)
        self.assertEqual(body["status"], 403)


@override_settings(
    TENANT_EXEMPT_PATHS=[
        "/admin/",
        "/healthz",
        "/readyz",
        "/api/schema/",
        "/api/docs/",
        "/api/redoc/",
    ]
)
class TenantMiddlewareExemptPathTests(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.middleware = TenantMiddleware(_simple_view)

    def test_healthz_exempt_no_header_required(self) -> None:
        req = self.factory.get("/healthz")
        response = self.middleware(req)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(req.tenant)

    def test_admin_login_exempt(self) -> None:
        req = self.factory.get("/admin/login/")
        response = self.middleware(req)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(req.tenant)

    def test_api_schema_exempt(self) -> None:
        req = self.factory.get("/api/schema/")
        response = self.middleware(req)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(req.tenant)

    def test_api_docs_exempt(self) -> None:
        req = self.factory.get("/api/docs/")
        response = self.middleware(req)
        self.assertEqual(response.status_code, 200)

    def test_readyz_exempt(self) -> None:
        req = self.factory.get("/readyz")
        response = self.middleware(req)
        self.assertEqual(response.status_code, 200)


class TenantMiddlewareContextVarResetTests(TestCase):
    """Ensure the ContextVar is fully reset after each request.

    This guards against tenant leakage between requests that are processed on
    the same worker/coroutine.
    """

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.tenant = _make_tenant("leak-test.example.com")

    def test_context_var_reset_after_response(self) -> None:
        ctx_after: list[Tenant | None] = []

        def view_that_checks_context(request: HttpRequest) -> HttpResponse:
            return HttpResponse("ok")

        mw = TenantMiddleware(view_that_checks_context)

        # First request — sets tenant context
        req1 = self.factory.get("/api/v1/")
        req1.META["HTTP_X_TENANT_DOMAIN"] = "leak-test.example.com"
        mw(req1)

        # After the first request completes, the ContextVar should be reset
        # (no active tenant in this thread/coroutine outside the middleware).
        ctx_after.append(get_current_tenant())
        self.assertIsNone(ctx_after[0], "ContextVar was not reset after the response")

    def test_second_request_without_header_returns_400(self) -> None:
        """A second request on the same worker without a header must not
        inherit the first request's tenant — it must return 400."""

        mw = TenantMiddleware(_simple_view)

        req1 = self.factory.get("/api/v1/")
        req1.META["HTTP_X_TENANT_DOMAIN"] = "leak-test.example.com"
        r1 = mw(req1)
        self.assertEqual(r1.status_code, 200)

        req2 = self.factory.get("/api/v1/")  # no header
        r2 = mw(req2)
        self.assertEqual(r2.status_code, 400)
