from types import SimpleNamespace

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from shop.middleware import RequestLoggingMiddleware


class RequestLoggingTests(SimpleTestCase):
    def test_request_logging_uses_only_safe_fields(self):
        secrets = ["sentinel-password", "sentinel-jwt", "sentinel-query", "sentinel-path", "sentinel-exception"]
        request = RequestFactory().post(
            f"/{secrets[3]}/?token={secrets[2]}",
            {"password": secrets[0]},
            HTTP_AUTHORIZATION=f"Bearer {secrets[1]}",
        )
        middleware = RequestLoggingMiddleware(lambda request: HttpResponse())
        response = HttpResponse(status=200)
        with self.assertNoLogs("shop.middleware", level="DEBUG"):
            self.assertIs(middleware.process_response(request, response), response)

        request.resolver_match = SimpleNamespace(view_name="checkout")
        response = HttpResponse(status=503)
        with self.assertLogs("shop.middleware", level="WARNING") as resolved:
            self.assertIs(middleware.process_response(request, response), response)
        self.assertIn("POST", resolved.output[0])
        self.assertIn("checkout", resolved.output[0])
        self.assertIn("503", resolved.output[0])

        request.resolver_match = None
        response = HttpResponse(status=404)
        with self.assertLogs("shop.middleware", level="WARNING") as unresolved:
            self.assertIs(middleware.process_response(request, response), response)
        self.assertIn("POST", unresolved.output[0])
        self.assertIn("unresolved", unresolved.output[0])
        self.assertIn("404", unresolved.output[0])

        with self.assertLogs("shop.middleware", level="ERROR") as exception:
            self.assertIsNone(middleware.process_exception(request, RuntimeError(secrets[4])))
        self.assertIn("RuntimeError", exception.output[0])
        records = resolved.records + unresolved.records + exception.records
        for record in records:
            self.assertIsNone(record.exc_info)
            for secret in secrets:
                self.assertNotIn(secret, record.getMessage())
