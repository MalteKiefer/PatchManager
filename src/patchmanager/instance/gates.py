"""View helpers for edition features."""

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import render
from django.utils.translation import gettext as _

from patchmanager.instance import editions

View = Callable[..., HttpResponseBase]


def not_in_edition(request: HttpRequest, feature: str) -> HttpResponse:
    """A friendly 403 page naming the missing feature."""
    title = _("%(feature)s is not part of this edition.") % {"feature": editions.FEATURES[feature]}
    return render(request, "core/error.html", {"title": title, "code": 403}, status=403)


def feature_required(feature: str) -> Callable[[View], View]:
    """Answer 403 for views of features the edition does not offer."""

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            if not editions.enabled(feature):
                return not_in_edition(request, feature)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
