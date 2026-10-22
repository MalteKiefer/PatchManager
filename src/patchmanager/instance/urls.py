from django.urls import path

from patchmanager.instance import views

app_name = "instance"
urlpatterns = [
    path("manage/branding/", views.branding, name="branding"),
    path("manage/settings/", views.settings_view, name="settings-index"),
    path("manage/settings/<slug:section>/", views.settings_view, name="settings"),
    path("branding.css", views.branding_css, name="css"),
    path("branding/logo.png", views.logo, name="logo"),
]
