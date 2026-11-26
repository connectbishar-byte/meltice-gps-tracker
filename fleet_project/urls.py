from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from tracking import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("live/", views.live, name="live"),
    path("history/", views.history, name="history"),
    path("records/", views.records, name="records"),
    path("records/<int:pk>/", views.record_detail, name="record_detail"),
    path("io/", views.io_inspector, name="io_inspector"),
    path("raw/", views.raw_packets, name="raw"),
    path("raw/<int:pk>/", views.raw_detail, name="raw_detail"),
    path("devices/", views.devices, name="devices"),
    path("devices/<int:pk>/", views.device_detail, name="device_detail"),
    path("connectivity/", views.connectivity, name="connectivity"),
    path("connectivity/selftest/", views.run_selftest, name="run_selftest"),
    path("settings/", views.settings_page, name="settings"),
    path("terminal/", views.terminal, name="terminal"),
    path("account/password/", views.PasswordChange.as_view(), name="password_change"),
    path("api/v1/", include("tracking.api_urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="api_schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="api_schema"), name="api_docs"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="api_schema"), name="api_redoc"),
    path("api/fleet/", views.api_fleet, name="api_fleet"),
    path("api/history/", views.api_history, name="api_history"),
    path("api/receiver/", views.api_receiver, name="api_receiver"),
    path("login/", views.ThrottledLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
]
