"""Search page and saved searches."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from patchmanager.history.views import object_url
from patchmanager.search import services
from patchmanager.search.models import SavedSearch

MAX_SAVED = 50
TITLES = {**{k: s.title for k, s in services.SOURCES.items()}, "note": gettext_lazy("Notes")}


def _kind(value: str) -> str:
    return value if value in TITLES else ""


def _url(hit: services.Hit) -> str:
    return object_url(hit.obj._meta.label_lower, hit.obj.pk)


@login_required
def search(request: HttpRequest) -> HttpResponse:
    """Results for ``q``, optionally limited to one type."""
    query = request.GET.get("q", "").strip()[: services.MAX_QUERY]
    kind = _kind(request.GET.get("type", ""))
    results, counts = services.search(
        request.user, getattr(request, "tenant", None), query, kind=kind, limit=100 if kind else 10
    )
    groups = [
        {"kind": k, "title": TITLES[k], "hits": [{"hit": h, "url": _url(h)} for h in hits]}
        for k, hits in results.items()
    ]
    facets = [{"kind": k, "title": TITLES[k], "count": n} for k, n in counts.items()]
    return render(request, "search/results.html", {
        "query": query, "kind": kind, "groups": groups, "facets": facets, "types": TITLES,
        "saved": request.user.saved_searches.all(),  # type: ignore[union-attr]
        "cap": services.CANDIDATES,
    })


@login_required
@require_POST
def save(request: HttpRequest) -> HttpResponse:
    """Store the current query under a name."""
    name = request.POST.get("name", "").strip()[:100]
    query = request.POST.get("q", "").strip()[: services.MAX_QUERY]
    kind = _kind(request.POST.get("type", ""))
    if not name or len(query) < 2:
        messages.error(request, _("Enter a name and a search term."))
    elif request.user.saved_searches.count() >= MAX_SAVED:  # type: ignore[union-attr]
        messages.error(request, _("You can keep up to %(n)s saved searches.") % {"n": MAX_SAVED})
    else:
        try:
            SavedSearch.objects.create(user=request.user, name=name, query=query, kind=kind)
            messages.success(request, _("Search saved."))
        except IntegrityError:
            messages.error(request, _("A saved search with this name exists."))
    return redirect(reverse("search:search") + "?" + urlencode({"q": query, "type": kind}))


@login_required
@require_POST
def delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Remove one of the user's saved searches."""
    get_object_or_404(SavedSearch, pk=pk, user=request.user).delete()
    return redirect("search:search")
