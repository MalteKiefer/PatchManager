from django.urls import path

from patchmanager.networks import unifi_import, views

app_name = "networks"

urlpatterns = [
    path("unifi/import/", unifi_import.import_view, name="unifi-import"),
    path("vlans/", views.vlan_list, name="vlans"),
    path("vlans/new/", views.vlan_create, name="vlan-create"),
    path("vlans/<uuid:pk>/", views.vlan_detail, name="vlan"),
    path("wlans/", views.wlan_list, name="wlans"),
    path("wlans/new/", views.wlan_create, name="wlan-create"),
    path("wlans/<uuid:pk>/", views.wlan_detail, name="wlan"),
    path("stacks/", views.stack_list, name="stacks"),
    path("stacks/new/", views.stack_create, name="stack-create"),
    path("stacks/<uuid:pk>/", views.stack_detail, name="stack"),
    path("stacks/<uuid:pk>/members/new/", views.member_create, name="member-create"),
]
