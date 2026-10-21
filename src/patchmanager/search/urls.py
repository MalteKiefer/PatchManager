from django.urls import path

from patchmanager.search import views

app_name = "search"
urlpatterns = [
    path("", views.search, name="search"),
    path("saved/", views.save, name="save"),
    path("saved/<uuid:pk>/delete/", views.delete, name="delete"),
]
