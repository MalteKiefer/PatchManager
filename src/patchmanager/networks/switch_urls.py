from django.urls import path

from patchmanager.networks import switch_views

app_name = "switching"

urlpatterns = [
    path("<uuid:pk>/network/", switch_views.overview, name="overview"),
    path("<uuid:pk>/network/read/", switch_views.read, name="read"),
    path("<uuid:pk>/network/config.txt", switch_views.export, name="export"),
]
