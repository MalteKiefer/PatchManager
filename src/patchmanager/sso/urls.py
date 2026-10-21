from django.urls import path

from patchmanager.sso import views

app_name = "sso"
urlpatterns = [
    path("sso/<slug:slug>/login/", views.login, name="login"),
    path("sso/<slug:slug>/oidc/callback/", views.oidc_callback, name="oidc-callback"),
    path("sso/<slug:slug>/saml/acs/", views.saml_acs, name="saml-acs"),
    path("sso/<slug:slug>/saml/metadata/", views.saml_metadata, name="saml-metadata"),
    path("sso/<slug:slug>/saml/sls/", views.saml_sls, name="saml-sls"),
    path("manage/sso/", views.provider_list, name="providers"),
    path("manage/sso/new/", views.provider_edit, name="provider-create"),
    path("manage/sso/<uuid:pk>/", views.provider_edit, name="provider"),
    path("manage/sso/<uuid:pk>/delete/", views.provider_delete, name="provider-delete"),
]
