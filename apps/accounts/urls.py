from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("no-access/", views.no_access, name="no_access"),
    path("healthz/", views.healthz, name="healthz"),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/<int:pk>/roles/", views.user_roles, name="user_roles"),
]
