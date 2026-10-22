from django.urls import path

from patchmanager.library import views

app_name = "library"

urlpatterns = [
    path("", views.type_list, name="types"),
    path("new/", views.type_create, name="type-create"),
    path("catalog/", views.catalog_search, name="catalog"),
    path("catalog/<int:pk>/", views.catalog_adopt, name="catalog-entry"),
    path("import/", views.type_import, name="type-import"),
    path("<uuid:pk>/", views.type_detail, name="type"),
    path("<uuid:pk>/ports/new/", views.type_ports_create, name="type-ports-create"),
    path("<uuid:pk>/export/", views.type_export, name="type-export"),
]
