from django.urls import include, path

from routeplanner import views

urlpatterns = [
    path("", views.map_view, name="home"),
    path("api/", include("routeplanner.urls")),
]
