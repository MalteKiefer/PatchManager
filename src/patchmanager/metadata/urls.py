from django.urls import path

from patchmanager.metadata import views

app_name = "metadata"

urlpatterns = [
    path("<str:label>/<uuid:object_id>/upload/", views.upload, name="upload"),
    path("<str:label>/<uuid:object_id>/note/", views.note, name="note"),
    path("files/<uuid:pk>/", views.download, name="download"),
    path("files/<uuid:pk>/delete/", views.remove, name="remove"),
    path("notes/<uuid:pk>/delete/", views.remove_note, name="remove-note"),
]
