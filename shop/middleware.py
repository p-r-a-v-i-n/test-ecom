import logging

from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        if response.status_code >= 400:
            match = getattr(request, "resolver_match", None)
            logger.warning(
                "request_error method=%s view=%s status=%s",
                request.method,
                match.view_name if match else "unresolved",
                response.status_code,
            )
        return response

    def process_exception(self, request, exception):
        logger.error("request_exception error_type=%s", type(exception).__name__)
