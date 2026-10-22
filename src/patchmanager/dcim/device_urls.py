from django.urls import path

from patchmanager.dcim import device_views

app_name = "devices"

urlpatterns = [
    path("new/", device_views.device_create, name="device-create"),
    path("<uuid:pk>/", device_views.device_detail, name="device"),
    path("<uuid:pk>/ports/new/", device_views.port_create, name="port-create"),
    path("ports/<uuid:pk>/", device_views.port_detail, name="port"),
]
