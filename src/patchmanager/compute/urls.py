from django.urls import path

from patchmanager.compute import connections, importing

app_name = "compute"

urlpatterns = [
    path("import/", importing.import_view, name="import"),
    path("connections/", connections.connection_list, name="connections"),
    path("connections/new/", connections.connection_create, name="connection-create"),
    path("connections/<uuid:pk>/", connections.connection_detail, name="connection"),
    path("connections/<uuid:pk>/select/", connections.connection_select, name="connection-select"),
]
