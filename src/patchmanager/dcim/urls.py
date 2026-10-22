from django.urls import path

from patchmanager.dcim import views

app_name = "dcim"

urlpatterns = [
    path("<uuid:pk>/", views.rack_detail, name="rack"),
    path("<uuid:pk>/spaces/new/", views.space_create, name="space-create"),
    path("<uuid:pk>/spaces/<uuid:space_pk>/move/", views.space_move, name="space-move"),
    path("<uuid:pk>/spaces/<uuid:space_pk>/delete/", views.space_delete, name="space-delete"),
]
