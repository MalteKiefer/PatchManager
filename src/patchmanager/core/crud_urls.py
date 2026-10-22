from django.urls import path

from patchmanager.core import crud

app_name = "objects"

urlpatterns = [
    path("<slug:key>/", crud.index, name="index"),
    path("<slug:key>/new/", crud.new, name="new"),
    path("<slug:key>/<uuid:pk>/", crud.detail, name="detail"),
]
