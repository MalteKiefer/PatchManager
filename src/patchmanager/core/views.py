"""Liveness and readiness endpoints for orchestrators and load balancers."""

import logging

from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.db.utils import DatabaseError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from patchmanager import __version__

logger = logging.getLogger(__name__)


@transaction.non_atomic_requests
@never_cache
@require_GET
def live(request: HttpRequest) -> JsonResponse:
    """Process is up. Does not touch dependencies."""
    return JsonResponse({"status": "ok", "version": __version__})


@transaction.non_atomic_requests
@never_cache
@require_GET
def ready(request: HttpRequest) -> JsonResponse:
    """Process can serve traffic: the database answers."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        logger.exception("readiness check failed: database unavailable")
        return JsonResponse({"status": "unavailable", "checks": {"database": "fail"}}, status=503)
    return JsonResponse({"status": "ok", "checks": {"database": "ok"}})


@login_required
def home(request: HttpRequest) -> HttpResponse:
    """Landing page after login."""
    return render(request, "core/home.html")


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Plain error page; the reason is logged, not shown."""
    logger.warning("csrf failure", extra={"reason": reason, "path": request.path})
    return render(
        request, "core/error.html", {"title": "Request rejected", "code": 403}, status=403
    )


def forbidden(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """403 page."""
    return render(
        request, "core/error.html", {"title": "Permission denied", "code": 403}, status=403
    )


def not_found(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """404 page."""
    return render(request, "core/error.html", {"title": "Not found", "code": 404}, status=404)
