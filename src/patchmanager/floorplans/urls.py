from django.urls import path

from patchmanager.floorplans import views

app_name = "floorplans"

urlpatterns = [
    path("<uuid:floor_pk>/", views.editor, name="editor"),
    path("<uuid:floor_pk>/upload/", views.upload, name="upload"),
    path("<uuid:floor_pk>/scale/", views.scale, name="scale"),
    path("<uuid:floor_pk>/outline/", views.outline, name="outline"),
    path("<uuid:floor_pk>/place/", views.place, name="place"),
    path("<uuid:floor_pk>/unplace/", views.unplace, name="unplace"),
]
