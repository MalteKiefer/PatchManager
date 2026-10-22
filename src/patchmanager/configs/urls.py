from django.urls import path

from patchmanager.configs import views

app_name = "configs"

urlpatterns = [
    path("<uuid:pk>/configs/", views.config_list, name="list"),
    path("<uuid:pk>/configs/new/", views.config_create, name="create"),
    path("<uuid:pk>/configs/diff/", views.config_diff, name="diff"),
    path("<uuid:pk>/configs/<int:version>/", views.config_version, name="version"),
    path("<uuid:pk>/configs/<int:version>/download/", views.config_download, name="download"),
    path("<uuid:pk>/configs/<int:version>/delete/", views.config_delete, name="delete"),
]
