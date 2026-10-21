"""Prometheus metrics (docs/adr/0010).

``/metrics`` is blocked at the reverse proxy and additionally requires the
bearer token from ``PATCHMANAGER_METRICS_TOKEN``; without a token configured
it answers 404. Under gunicorn set ``PROMETHEUS_MULTIPROC_DIR`` so all worker
processes are aggregated.
"""

import hmac
import os
import time
from collections.abc import Callable

from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.views.decorators.http import require_GET
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

REQUESTS = Counter("patchmanager_http_requests_total", "HTTP requests", ["method", "status"])
LATENCY = Histogram("patchmanager_http_request_seconds", "HTTP request latency", ["method"])
LOGINS = Counter("patchmanager_logins_total", "Login attempts", ["outcome"])


class MetricsMiddleware:
    """Count requests and measure latency. Labels stay low cardinality."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Standard middleware constructor."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Time the request."""
        start = time.perf_counter()
        response = self.get_response(request)
        method = (
            request.method
            if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            else "OTHER"
        )
        LATENCY.labels(method).observe(time.perf_counter() - start)
        REQUESTS.labels(method, str(response.status_code)).inc()
        return response


@transaction.non_atomic_requests
@require_GET
def metrics_view(request: HttpRequest) -> HttpResponse:
    """Expose metrics to a scraper holding the configured token."""
    token = os.environ.get("PATCHMANAGER_METRICS_TOKEN", "")
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not token or not hmac.compare_digest(token, supplied):
        raise Http404
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        payload = generate_latest(registry)
    else:
        payload = generate_latest()
    return HttpResponse(payload, content_type=CONTENT_TYPE_LATEST)
