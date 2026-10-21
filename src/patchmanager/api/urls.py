from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView
from rest_framework.routers import DefaultRouter

from patchmanager.api import oauth, views

router = DefaultRouter(trailing_slash=True)
for prefix, viewset in (
    ("tenants", views.TenantViewSet),
    ("sites", views.SiteViewSet),
    ("buildings", views.BuildingViewSet),
    ("floors", views.FloorViewSet),
    ("rooms", views.RoomViewSet),
    ("racks", views.RackViewSet),
    ("vendors", views.VendorViewSet),
    ("device-types", views.DeviceTypeViewSet),
    ("devices", views.DeviceViewSet),
    ("ports", views.PortViewSet),
    ("cables", views.CableViewSet),
    ("vlans", views.VLANViewSet),
    ("wlans", views.WLANViewSet),
    ("changes", views.ChangeViewSet),
):
    router.register(prefix, viewset, basename=prefix)

app_name = "api"
urlpatterns = [
    path("v1/", include(router.urls)),
    path("v1/search/", views.search, name="search"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("oauth/token/", oauth.token, name="oauth-token"),
]
