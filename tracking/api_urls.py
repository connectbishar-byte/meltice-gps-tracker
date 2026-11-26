from django.urls import path
from rest_framework.routers import DefaultRouter

from . import api

router = DefaultRouter()
router.register("devices", api.DeviceViewSet, basename="device")
router.register("records", api.RecordViewSet, basename="record")
router.register("packets", api.PacketViewSet, basename="packet")
router.register("connections", api.ConnectionViewSet, basename="connection")
router.register("tests", api.TestRunViewSet, basename="testrun")

urlpatterns = [
    path("auth/token/", api.TokenView.as_view(), name="api_token"),
    path("auth/me/", api.me, name="api_me"),
    path("port-events/", api.port_events, name="api_port_events"),
    path("receiver/", api.receiver, name="api_v1_receiver"),
] + router.urls
