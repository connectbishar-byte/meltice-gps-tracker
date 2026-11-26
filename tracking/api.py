"""
REST API v1 (Django REST Framework).

Authentication: the same user accounts as the web UI.
  * Browser: session cookie (log in at /login/).
  * Scripts / other systems: POST /api/v1/auth/token/ {"username", "password"} -> {"token"},
    then send the header `Authorization: Token <token>`.
Roles:
  * any signed-in user: read devices, records, connections, receiver status
  * raw-data users (staff, superusers or members of the "raw_data" group): raw packets and the port event stream
  * staff users: create/edit devices and run connectivity tests
Interactive documentation: /api/docs/ (Swagger UI) and /api/redoc/.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import mixins, permissions, serializers, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import codec
from .models import AVLRecord, Connection, Device, IOValue, PortEvent, RawPacket, TestRun, mask_imei
from .views import can_view_raw, device_status, receiver_status


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
class CanViewRaw(permissions.BasePermission):
    message = "Raw data needs a staff account or membership of the 'raw_data' group."

    def has_permission(self, request, view):
        return can_view_raw(request.user)


class StaffWriteOrReadOnly(permissions.BasePermission):
    message = "Only staff users can change this."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.method in permissions.SAFE_METHODS or request.user.is_staff


def _dt(value: str | None, name: str):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        raise ValidationError({name: "Use ISO 8601, e.g. 2026-11-26T08:00:00Z"})
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class UserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    email = serializers.CharField()
    is_staff = serializers.BooleanField()
    is_superuser = serializers.BooleanField()
    can_view_raw = serializers.SerializerMethodField()

    def get_can_view_raw(self, user) -> bool:
        return can_view_raw(user)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(style={"input_type": "password"}, trim_whitespace=False)


class TokenView(APIView):
    """Exchange a username and password for an API token (or revoke it with DELETE)."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def get_permissions(self):
        if self.request.method == "DELETE":
            return [permissions.IsAuthenticated()]
        return super().get_permissions()

    @extend_schema(request=LoginSerializer, auth=[], responses=inline_serializer(
        "TokenResponse", {"token": serializers.CharField(), "user": UserSerializer()}))
    def post(self, request):
        s = LoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = authenticate(request, username=s.validated_data["username"], password=s.validated_data["password"])
        if user is None or not user.is_active:
            return Response({"detail": "Invalid username or password."}, status=status.HTTP_401_UNAUTHORIZED)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "user": UserSerializer(user).data})

    @extend_schema(request=None, responses={204: None})
    def delete(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(responses=UserSerializer)
@api_view(["GET"])
def me(request):
    """The signed-in user and their roles."""
    return Response(UserSerializer(request.user).data)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
class DeviceSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = ["id", "imei", "name", "plate", "group", "model", "firmware", "config_revision", "enabled", "notes",
                  "created_utc", "status"]
        read_only_fields = ["created_utc"]

    def get_status(self, obj) -> dict:
        s = device_status(obj)
        s.pop("imei", None)
        return s

    def validate_imei(self, value):
        value = value.strip()
        if not value.isdigit() or not 14 <= len(value) <= 17:
            raise serializers.ValidationError("IMEI must be 14-17 digits (normally 15).")
        return value

    def to_representation(self, obj):
        data = super().to_representation(obj)
        req = self.context.get("request")
        if not (req and req.user.is_staff):
            data["imei"] = mask_imei(obj.imei)
        return data


class HistoryPointSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    ts = serializers.DateTimeField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    speed = serializers.IntegerField()
    angle = serializers.IntegerField()
    alt = serializers.IntegerField()
    sats = serializers.IntegerField()
    valid = serializers.BooleanField()
    late = serializers.BooleanField()


class DeviceViewSet(viewsets.ModelViewSet):
    """Registered trackers. Only enabled IMEIs on this list are accepted by the TCP receiver."""
    queryset = Device.objects.all()
    serializer_class = DeviceSerializer
    permission_classes = [StaffWriteOrReadOnly]
    pagination_class = None

    @extend_schema(parameters=[
        OpenApiParameter("from", OpenApiTypes.DATETIME, description="Default: 24 hours ago"),
        OpenApiParameter("to", OpenApiTypes.DATETIME, description="Default: now"),
        OpenApiParameter("valid_only", OpenApiTypes.BOOL)], responses=HistoryPointSerializer(many=True))
    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        """Route points for one device, oldest first (max 20,000)."""
        dev = self.get_object()
        now = timezone.now()
        start = _dt(request.query_params.get("from"), "from") or now - timedelta(hours=24)
        end = _dt(request.query_params.get("to"), "to") or now
        qs = dev.records.filter(timestamp_utc__gte=start, timestamp_utc__lte=end)
        if request.query_params.get("valid_only") in ("1", "true", "yes"):
            qs = qs.filter(valid_fix=True)
        pts = [{"id": r.id, "ts": r.timestamp_utc, "lat": r.latitude, "lon": r.longitude, "speed": r.speed,
                "angle": r.angle, "alt": r.altitude, "sats": r.satellites, "valid": r.valid_fix, "late": r.late}
               for r in qs.order_by("timestamp_utc", "id")[:20000]]
        return Response(HistoryPointSerializer(pts, many=True).data)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
class IOValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = IOValue
        fields = ["avl_id", "name", "byte_length", "raw_hex", "value", "text", "unit", "known"]


class RecordSerializer(serializers.ModelSerializer):
    device_name = serializers.CharField(source="device.name", read_only=True)
    io_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = AVLRecord
        fields = ["id", "device", "device_name", "packet", "index", "timestamp_utc", "received_utc", "latitude",
                  "longitude", "altitude", "angle", "satellites", "speed", "priority", "event_io_id", "valid_fix",
                  "late", "io_count"]


class RecordDetailSerializer(RecordSerializer):
    io = IOValueSerializer(many=True, read_only=True)

    class Meta(RecordSerializer.Meta):
        fields = RecordSerializer.Meta.fields + ["io"]


@extend_schema(parameters=[
    OpenApiParameter("device", OpenApiTypes.INT), OpenApiParameter("from", OpenApiTypes.DATETIME),
    OpenApiParameter("to", OpenApiTypes.DATETIME), OpenApiParameter("valid_fix", OpenApiTypes.BOOL),
    OpenApiParameter("late", OpenApiTypes.BOOL)])
class RecordViewSet(viewsets.ReadOnlyModelViewSet):
    """Decoded AVL records (newest first), each with its I/O elements on the detail endpoint."""
    serializer_class = RecordSerializer

    def get_queryset(self):
        q = self.request.query_params
        qs = AVLRecord.objects.select_related("device").annotate(io_count=Count("io")).order_by("-timestamp_utc", "-id")
        if self.action == "retrieve":
            return AVLRecord.objects.select_related("device").annotate(io_count=Count("io")).prefetch_related("io")
        if q.get("device"):
            qs = qs.filter(device_id=q["device"])
        if q.get("from"):
            qs = qs.filter(timestamp_utc__gte=_dt(q["from"], "from"))
        if q.get("to"):
            qs = qs.filter(timestamp_utc__lte=_dt(q["to"], "to"))
        if q.get("valid_fix") in ("true", "1", "false", "0"):
            qs = qs.filter(valid_fix=q["valid_fix"] in ("true", "1"))
        if q.get("late") in ("true", "1"):
            qs = qs.filter(late=True)
        return qs

    def get_serializer_class(self):
        return RecordDetailSerializer if self.action == "retrieve" else RecordSerializer


# ---------------------------------------------------------------------------
# Raw packets
# ---------------------------------------------------------------------------
class PacketSerializer(serializers.ModelSerializer):
    device_name = serializers.CharField(source="device.name", read_only=True, default=None)
    remote = serializers.CharField(source="connection.remote", read_only=True, default=None)

    class Meta:
        model = RawPacket
        fields = ["id", "received_utc", "device", "device_name", "connection", "remote", "size", "codec", "crc_ok",
                  "crc_value", "record_count", "new_records", "duplicate_records", "parse_status", "error",
                  "ack_status", "ack_value", "truncated", "sha256", "hex"]


class PacketDetailSerializer(PacketSerializer):
    decoded = serializers.SerializerMethodField()

    class Meta(PacketSerializer.Meta):
        fields = PacketSerializer.Meta.fields + ["decoded"]

    def get_decoded(self, obj) -> dict | None:
        if obj.truncated:
            return {"error": "stored HEX is truncated; frame cannot be re-decoded"}
        try:
            pkt = codec.decode_frame(bytes.fromhex(obj.hex))
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {
            "codec": pkt.codec_name, "crc": f"{pkt.crc & 0xFFFF:04X}",
            "records": [{
                "timestamp_ms": r.timestamp_ms, "priority": r.priority, "latitude": r.latitude,
                "longitude": r.longitude, "altitude": r.altitude, "angle": r.angle, "satellites": r.satellites,
                "speed": r.speed, "event_io_id": r.event_io_id, "valid_fix": r.has_valid_fix,
                "io": [{"id": e.avl_id, "bytes": e.length, "hex": e.raw.hex().upper()} for e in r.io],
            } for r in pkt.records],
        }


@extend_schema(parameters=[
    OpenApiParameter("device", OpenApiTypes.INT), OpenApiParameter("status", OpenApiTypes.STR, enum=["ok", "error"]),
    OpenApiParameter("codec", OpenApiTypes.STR), OpenApiParameter("search", OpenApiTypes.STR,
                                                                  description="Text in HEX or error message")])
class PacketViewSet(viewsets.ReadOnlyModelViewSet):
    """Every Teltonika frame received, valid or not, as stored HEX with CRC / parse / ACK status."""
    permission_classes = [CanViewRaw]

    def get_queryset(self):
        q = self.request.query_params
        qs = RawPacket.objects.select_related("device", "connection").order_by("-received_utc", "-id")
        if q.get("device"):
            qs = qs.filter(device_id=q["device"])
        if q.get("status") in ("ok", "error"):
            qs = qs.filter(parse_status=q["status"])
        if q.get("codec"):
            qs = qs.filter(codec=q["codec"])
        if q.get("search"):
            qs = qs.filter(Q(hex__icontains=q["search"]) | Q(error__icontains=q["search"]))
        return qs

    def get_serializer_class(self):
        return PacketDetailSerializer if self.action == "retrieve" else PacketSerializer


# ---------------------------------------------------------------------------
# Connections and port events
# ---------------------------------------------------------------------------
class ConnectionSerializer(serializers.ModelSerializer):
    device_name = serializers.CharField(source="device.name", read_only=True, default=None)

    class Meta:
        model = Connection
        fields = ["id", "device", "device_name", "claimed_imei", "remote", "source", "opened_utc", "closed_utc",
                  "handshake", "frames", "close_reason"]

    def to_representation(self, obj):
        data = super().to_representation(obj)
        req = self.context.get("request")
        if not (req and can_view_raw(req.user)):
            data["claimed_imei"] = mask_imei(obj.claimed_imei)
        return data


class ConnectionViewSet(viewsets.ReadOnlyModelViewSet):
    """TCP connections to the tracker port, including rejected and invalid handshakes."""
    serializer_class = ConnectionSerializer

    def get_queryset(self):
        qs = Connection.objects.select_related("device").order_by("-opened_utc", "-id")
        h = self.request.query_params.get("handshake")
        return qs.filter(handshake=h) if h else qs


class PortEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = PortEvent
        fields = ["id", "ts", "kind", "level", "remote", "imei", "connection", "packet", "size", "hex", "truncated",
                  "message"]


@extend_schema(parameters=[
    OpenApiParameter("after", OpenApiTypes.INT, description="Only events with id greater than this (live tail)"),
    OpenApiParameter("before", OpenApiTypes.INT, description="Only events with id smaller than this (scroll back)"),
    OpenApiParameter("tail", OpenApiTypes.INT, description="Without after/before: return the newest N (default 200)"),
    OpenApiParameter("limit", OpenApiTypes.INT, description="Max events per call (default 500, max 2000)"),
    OpenApiParameter("kind", OpenApiTypes.STR, description="Comma list: connect,refused,rx,tx,imei,frame,error,close"),
    OpenApiParameter("imei", OpenApiTypes.STR), OpenApiParameter("remote", OpenApiTypes.STR,
                                                                 description="IP or IP:port, partial match"),
    OpenApiParameter("q", OpenApiTypes.STR, description="Text in message, HEX, IMEI or remote")],
    responses=inline_serializer("PortEventPage", {
        "events": PortEventSerializer(many=True), "last_id": serializers.IntegerField(),
        "receiver": serializers.DictField()}))
@api_view(["GET"])
@permission_classes([CanViewRaw])
def port_events(request):
    """
    Everything that happened on the tracker TCP port, oldest first: connections, every byte received
    (rx) and sent (tx) as HEX, IMEI handshakes, decoded frame summaries, ACKs, errors and disconnects.
    Poll with `after=<last_id>` for a live tail.
    """
    q = request.query_params
    try:
        limit = max(1, min(2000, int(q.get("limit") or 500)))
        after = int(q["after"]) if q.get("after") else None
        before = int(q["before"]) if q.get("before") else None
        tail = max(1, min(2000, int(q.get("tail") or 200)))
    except ValueError:
        raise ValidationError("after, before, tail and limit must be integers")
    qs = PortEvent.objects.all()
    if q.get("kind"):
        qs = qs.filter(kind__in=[k.strip() for k in q["kind"].split(",") if k.strip()])
    if q.get("imei"):
        qs = qs.filter(imei__contains=q["imei"].strip())
    if q.get("remote"):
        qs = qs.filter(remote__contains=q["remote"].strip())
    if q.get("q"):
        t = q["q"].strip()
        qs = qs.filter(Q(message__icontains=t) | Q(hex__icontains=t.replace(" ", "")) | Q(imei__contains=t) |
                       Q(remote__contains=t))
    if after is not None:
        rows = list(qs.filter(id__gt=after).order_by("id")[:limit])
    elif before is not None:
        rows = list(qs.filter(id__lt=before).order_by("-id")[:limit])[::-1]
    else:
        rows = list(qs.order_by("-id")[:tail])[::-1]
    last = PortEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0
    return Response({"events": PortEventSerializer(rows, many=True).data,
                     "last_id": rows[-1].id if rows else (after if after is not None else last),
                     "receiver": receiver_status()})


# ---------------------------------------------------------------------------
# Receiver + connectivity tests
# ---------------------------------------------------------------------------
@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
def receiver(request):
    """TCP receiver health (from its heartbeat) and the endpoint devices must be configured with."""
    cfg = settings.FLEET
    return Response(receiver_status() | {"public_endpoint": f"{cfg['PUBLIC_GPS_HOST']}:{cfg['PUBLIC_GPS_PORT']}",
                                         "port_log": cfg["PORT_LOG"],
                                         "auto_register_unknown": cfg["AUTO_REGISTER_UNKNOWN"]})


class TestRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestRun
        fields = ["id", "started_utc", "target", "passed", "summary", "steps"]


class RunTestSerializer(serializers.Serializer):
    host = serializers.CharField(required=False, help_text="Default: FLEET_SELFTEST_HOST")
    port = serializers.IntegerField(required=False, min_value=1, max_value=65535)
    mode = serializers.ChoiceField(choices=["full", "quick"], default="full")


class TestRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Connectivity self-tests: DNS, TCP, IMEI handshake, Codec 8/8E frames, CRC, fragmentation, replay."""
    queryset = TestRun.objects.all()
    serializer_class = TestRunSerializer

    @extend_schema(request=RunTestSerializer, responses=TestRunSerializer)
    @action(detail=False, methods=["post"], permission_classes=[permissions.IsAdminUser])
    def run(self, request):
        """Run the protocol self-test against a receiver (staff only). Takes a few seconds."""
        from .simulator import run_connectivity_suite
        s = RunTestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        cfg = settings.FLEET
        Device.objects.get_or_create(imei=cfg["SELFTEST_IMEI"],
                                     defaults={"name": "Self-test simulator", "model": "SIMULATOR", "group": "Test"})
        host = s.validated_data.get("host") or cfg["SELFTEST_HOST"]
        port = s.validated_data.get("port") or cfg["SELFTEST_PORT"]
        steps = run_connectivity_suite(host, port, cfg["SELFTEST_IMEI"], full=s.validated_data["mode"] == "full",
                                       timeout=8)
        passed = sum(x["ok"] for x in steps)
        run = TestRun.objects.create(target=f"{host}:{port}", passed=passed == len(steps), steps=steps,
                                     summary=f"{passed}/{len(steps)} steps passed")
        return Response(TestRunSerializer(run).data, status=status.HTTP_201_CREATED)
