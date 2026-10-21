from django.urls import path

from patchmanager.core import views

urlpatterns = [
    path("health/live", views.live, name="health-live"),
    path("health/ready", views.ready, name="health-ready"),
]
