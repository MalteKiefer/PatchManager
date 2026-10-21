from django.urls import path

from patchmanager.integrations import views

app_name = "integrations"
urlpatterns = [
    path("webhooks/", views.webhook_list, name="webhooks"),
    path("webhooks/<uuid:pk>/", views.webhook_detail, name="webhook"),
    path("webhooks/<uuid:pk>/<slug:action>/", views.webhook_action, name="webhook-action"),
    path("deliveries/<uuid:pk>/redeliver/", views.redeliver, name="redeliver"),
]
