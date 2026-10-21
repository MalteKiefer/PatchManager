from django.urls import path, re_path

from patchmanager.scim import views

app_name = "scim"
urlpatterns = [
    re_path(r"^scim/v2/Users/?$", views.users, name="users"),
    re_path(r"^scim/v2/Users/(?P<pk>[^/]+)/?$", views.user, name="user"),
    re_path(r"^scim/v2/Groups/?$", views.groups, name="groups"),
    re_path(r"^scim/v2/Groups/(?P<pk>[^/]+)/?$", views.group, name="group"),
    re_path(r"^scim/v2/ServiceProviderConfig/?$", views.service_provider_config, name="config"),
    re_path(r"^scim/v2/ResourceTypes/?$", views.resource_types, name="resource-types"),
    re_path(r"^scim/v2/Schemas/?$", views.schemas, name="schemas"),
    path("manage/scim/", views.token_list, name="tokens"),
    path("manage/scim/<uuid:pk>/revoke/", views.token_revoke, name="token-revoke"),
]
