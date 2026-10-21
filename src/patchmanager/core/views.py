"""Liveness and readiness endpoints for orchestrators and load balancers."""

import logging

from django.db import connection
from django.db.utils import DatabaseError
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from patchmanager import __version__

logger = logging.getLogger(__name__)


@never_cache
@require_GET
def live(request: HttpRequest) -> JsonResponse:
    """Process is up. Does not touch dependencies."""
    return JsonResponse({"status": "ok", "version": __version__})


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
