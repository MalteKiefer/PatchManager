from django.urls import path

from patchmanager.compute import importing

app_name = "compute"

urlpatterns = [
    path("import/", importing.import_view, name="import"),
]
