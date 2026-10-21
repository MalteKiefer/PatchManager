from django.conf import settings
from django.urls import include, path
from django.views.i18n import set_language

from patchmanager.core import metrics, views
from patchmanager.tenancy.views import switch

core_patterns = ([path("", views.home, name="home")], "core")

urlpatterns = [
    path("health/live", views.live, name="health-live"),
    path("health/ready", views.ready, name="health-ready"),
    path("metrics", metrics.metrics_view, name="metrics"),
    path("i18n/setlang/", set_language, name="set_language"),
    path("tenants/switch/", switch, name="tenant-switch"),
    path("manage/", include("patchmanager.manage_urls")),
    path("locations/", include("patchmanager.locations.urls")),
    path("racks/", include("patchmanager.dcim.urls")),
    path("library/", include("patchmanager.library.urls")),
    path("devices/", include("patchmanager.dcim.device_urls")),
    path("meta/", include("patchmanager.metadata.urls")),
    path("cabling/", include("patchmanager.cabling.urls")),
    path("networks/", include("patchmanager.networks.urls")),
    path("plans/", include("patchmanager.floorplans.urls")),
    path("history/", include("patchmanager.history.urls")),
    path("search/", include("patchmanager.search.urls")),
    path("exchange/", include("patchmanager.exchange.urls")),
    path("api/", include("patchmanager.api.urls")),
    path("", include("patchmanager.api.ui_urls")),
    path("integrations/", include("patchmanager.integrations.urls")),
    path("", include("patchmanager.accounts.urls")),
    path("", include(core_patterns)),
]

if settings.DJANGO_ADMIN_ENABLED:
    from django.contrib import admin

    urlpatterns.insert(0, path("django-admin/", admin.site.urls))

handler403 = "patchmanager.core.views.forbidden"
handler404 = "patchmanager.core.views.not_found"
