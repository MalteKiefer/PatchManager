from django.urls import path

from patchmanager.ipam import views

app_name = "ipam"

urlpatterns = [
    path("", views.prefix_list, name="prefixes"),
    path("prefixes/new/", views.prefix_create, name="prefix-create"),
    path("prefixes/<uuid:pk>/", views.prefix_detail, name="prefix"),
    path("addresses/", views.address_list, name="addresses"),
    path("addresses/new/", views.address_create, name="address-create"),
    path("addresses/<uuid:pk>/", views.address_detail, name="address"),
    path("vrfs/", views.vrf_list, name="vrfs"),
    path("vrfs/new/", views.vrf_create, name="vrf-create"),
    path("vrfs/<uuid:pk>/", views.vrf_detail, name="vrf"),
]
