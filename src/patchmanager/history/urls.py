from django.urls import path

from patchmanager.history import views

app_name = "history"
urlpatterns = [
    path("", views.activity, name="activity"),
    path("changes/<uuid:pk>/", views.change_detail, name="change"),
    path("changes/<uuid:pk>/restore/", views.restore, name="restore"),
    path("<str:object_type>/<uuid:pk>/", views.object_history, name="object"),
]
