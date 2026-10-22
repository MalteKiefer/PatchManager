from django.urls import path

from patchmanager.networks import views

app_name = "networks"

urlpatterns = [
    path("vlans/", views.vlan_list, name="vlans"),
    path("vlans/new/", views.vlan_create, name="vlan-create"),
    path("vlans/<uuid:pk>/", views.vlan_detail, name="vlan"),
    path("wlans/", views.wlan_list, name="wlans"),
    path("wlans/new/", views.wlan_create, name="wlan-create"),
    path("wlans/<uuid:pk>/", views.wlan_detail, name="wlan"),
]
