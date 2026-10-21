from django.urls import path

from patchmanager.exchange import views

app_name = "exchange"
urlpatterns = [
    path("", views.index, name="index"),
    path("import/", views.upload, name="upload"),
    path("import/apply/", views.apply, name="apply"),
    path("templates/<slug:kind>.<str:fmt>", views.template, name="template"),
    path("<slug:kind>.<str:fmt>", views.export, name="export"),
    path("reports/<slug:kind>/<uuid:pk>.pdf", views.report, name="report"),
]
