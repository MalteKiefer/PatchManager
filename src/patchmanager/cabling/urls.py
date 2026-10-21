from django.urls import path

from patchmanager.cabling import views

app_name = "cabling"

urlpatterns = [
    path("", views.cable_list, name="cables"),
    path("port-options/", views.port_options, name="port-options"),
    path("connect/<uuid:port_pk>/", views.connect, name="connect"),
    path("disconnect/<uuid:port_pk>/", views.disconnect_port, name="disconnect"),
    path("trace/<uuid:port_pk>/", views.trace_view, name="trace"),
    path("<uuid:pk>/", views.cable_detail, name="cable"),
]
