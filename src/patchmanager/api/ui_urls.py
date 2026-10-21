from django.urls import path

from patchmanager.api import ui

app_name = "api-ui"
urlpatterns = [
    path("account/tokens/", ui.token_list, name="tokens"),
    path("account/tokens/<uuid:pk>/revoke/", ui.token_revoke, name="token-revoke"),
    path("manage/api-clients/", ui.client_list, name="clients"),
    path("manage/api-clients/<uuid:pk>/<slug:action>/", ui.client_action, name="client-action"),
]
