from django.urls import path

from patchmanager.dcim import views as dcim_views
from patchmanager.locations import views

app_name = "locations"

urlpatterns = [
    path("", views.index, name="index"),
    path("sites/new/", views.site_create, name="site-create"),
    path("sites/<uuid:pk>/move/", views.site_move, name="site-move"),
    path("room/<uuid:pk>/cabling/", dcim_views.room_cabling, name="room-cabling"),
    path("room/<uuid:pk>/plan/", views.room_plan, name="room-plan"),
    path("<slug:level>/<uuid:pk>/", views.node_detail, name="node"),
    path("<slug:level>/<uuid:pk>/new/", views.node_create, name="node-create"),
]
