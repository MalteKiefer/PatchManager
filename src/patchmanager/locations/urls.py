from django.urls import path

from patchmanager.locations import views

app_name = "locations"

urlpatterns = [
    path("", views.index, name="index"),
    path("sites/new/", views.site_create, name="site-create"),
    path("sites/<uuid:pk>/move/", views.site_move, name="site-move"),
    path("<slug:level>/<uuid:pk>/", views.node_detail, name="node"),
    path("<slug:level>/<uuid:pk>/new/", views.node_create, name="node-create"),
]
