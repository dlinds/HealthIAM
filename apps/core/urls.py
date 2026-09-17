from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("search/", views.search, name="search"),
    path("history/", views.history_list, name="history_list"),
    path(
        "history/<str:app_label>/<str:model>/<int:pk>/",
        views.object_history,
        name="object_history",
    ),
]
