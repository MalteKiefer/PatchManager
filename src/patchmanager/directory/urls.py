from django.urls import path

from patchmanager.directory import links

app_name = "directory"

urlpatterns = [
    path("links/new/", links.link_create, name="link-create"),
    path("links/<uuid:pk>/delete/", links.link_delete, name="link-delete"),
]
