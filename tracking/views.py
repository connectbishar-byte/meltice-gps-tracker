from __future__ import annotations

import csv
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.urls import reverse_lazy
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from . import codec, io_dictionary
from .models import AVLRecord, Connection, Device, RawPacket, ReceiverHeartbeat, TestRun, mask_imei


def fleet_context(request):
    return {"FLEET": settings.FLEET, "FLEET_TZ": settings.TIME_ZONE, "can_view_raw": can_view_raw(request.user)}


def can_view_raw(user) -> bool:
    return bool(user and user.is_authenticated and
                (user.is_superuser or user.is_staff or user.groups.filter(name="raw_data").exists()))


raw_required = user_passes_test(can_view_raw)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------
def device_status(d: Device, now=None) -> dict:
    now = now or timezone.now()
    cfg = settings.FLEET
    last = d.last_packet_utc
    if not d.enabled:
        link = "disabled"
    elif last is None:
        link = "never"
    elif (now - last).total_seconds() <= cfg["STALE_AFTER_SECONDS"]:
        link = "online"
    elif (now - last).total_seconds() <= cfg["OFFLINE_AFTER_SECONDS"]:
        link = "stale"
    else:
        link = "offline"

    if link in ("never", "offline", "disabled"):
        state = "nosignal"
    elif d.last_movement and (d.last_fix_speed or 0) > 3:
        state = "moving"
    elif d.last_ignition:
        state = "idling"
    else:
        state = "parked"
    fix_age = (now - d.last_fix_ts).total_seconds() if d.last_fix_ts else None
    return {
        "id": d.id, "name": d.name, "plate": d.plate, "group": d.group or "Default", "model": d.model,
        "imei": d.masked_imei, "enabled": d.enabled, "link": link, "state": state,
        "last_packet": d.last_packet_utc.isoformat() if d.last_packet_utc else None,
        "last_record": d.last_record_ts.isoformat() if d.last_record_ts else None,
        "fix": None if d.last_fix_ts is None else {
            "ts": d.last_fix_ts.isoformat(), "lat": d.last_fix_lat, "lon": d.last_fix_lon,
            "speed": d.last_fix_speed, "angle": d.last_fix_angle, "sats": d.last_fix_sats,
            "age_s": int(fix_age),
        },
        "ignition": d.last_ignition, "movement": d.last_movement, "gsm": d.last_gsm_signal,
        "ext_v": d.last_ext_voltage, "packets": d.packet_count, "records": d.record_count,
        "errors": d.error_count, "last_error": d.last_error,
    }


def receiver_status():
    hb = ReceiverHeartbeat.objects.order_by("-last_beat_utc").first()
    if not hb:
        return {"up": False, "detail": "No heartbeat yet - is run_receiver started?"}
    age = (timezone.now() - hb.last_beat_utc).total_seconds()
    return {"up": age < 20, "age_s": int(age), "host": hb.host, "port": hb.port, "pid": hb.pid,
            "started": hb.started_utc.isoformat(), "active": hb.active_connections,
            "total": hb.total_connections, "frames_ok": hb.frames_ok, "frames_bad": hb.frames_bad,
            "detail": "listening" if age < 20 else f"last heartbeat {int(age)} s ago"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def _client_ip(request) -> str:
    if settings.FLEET_TRUST_X_FORWARDED_FOR:
        real = request.META.get("HTTP_X_REAL_IP", "").strip()  # set by our Nginx site, not spoofable through it
        if real:
            return real
    return request.META.get("REMOTE_ADDR", "")


class ThrottledLoginView(auth_views.LoginView):
    """Username + password sign-in with a lockout after repeated failures (per username and per IP)."""
    template_name = "tracking/login.html"
    redirect_authenticated_user = True

    def _keys(self):
        user = (self.request.POST.get("username") or "").strip().lower()[:150]
        ip = _client_ip(self.request)
        return [f"login-fail:user:{user}", f"login-fail:ip:{ip}"]

    def post(self, request, *args, **kwargs):
        limit = settings.LOGIN_MAX_FAILURES
        if any((cache.get(k) or 0) >= limit * (3 if ":ip:" in k else 1) for k in self._keys()):
            form = self.get_form()
            form.errors.clear()
            return self.render_to_response(self.get_context_data(
                form=form, locked=True, lockout_minutes=settings.LOGIN_LOCKOUT_MINUTES))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        ttl = settings.LOGIN_LOCKOUT_MINUTES * 60
        for k in self._keys():
            try:
                cache.add(k, 0, ttl)
                cache.incr(k)
            except ValueError:
                cache.set(k, 1, ttl)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete_many(self._keys()[:1])
        return super().form_valid(form)


class PasswordChange(auth_views.PasswordChangeView):
    template_name = "tracking/password_change.html"
    success_url = reverse_lazy("dashboard")

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {"nav": "account"}

    def form_valid(self, form):
        messages.success(self.request, "Password changed.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@login_required
def dashboard(request):
    now = timezone.now()
    devices = [device_status(d, now) for d in Device.objects.all()]
    counts = {k: sum(1 for d in devices if d["state"] == k) for k in ("moving", "idling", "parked", "nosignal")}
    links = {k: sum(1 for d in devices if d["link"] == k) for k in ("online", "stale", "offline", "never")}
    since = now - timedelta(hours=24)
    stats = {
        "packets_24h": RawPacket.objects.filter(received_utc__gte=since).count(),
        "errors_24h": RawPacket.objects.filter(received_utc__gte=since, parse_status="error").count(),
        "records_24h": AVLRecord.objects.filter(received_utc__gte=since).count(),
        "connections_24h": Connection.objects.filter(opened_utc__gte=since).count(),
        "rejected_24h": Connection.objects.filter(opened_utc__gte=since, handshake="rejected").count(),
    }
    return render(request, "tracking/dashboard.html", {
        "nav": "dashboard", "devices": devices, "counts": counts, "links": links, "stats": stats,
        "receiver": receiver_status(), "total": len(devices),
    })


@login_required
@raw_required
def terminal(request):
    cfg = settings.FLEET
    return render(request, "tracking/terminal.html", {
        "nav": "terminal", "receiver": receiver_status(),
        "public_endpoint": f"{cfg['PUBLIC_GPS_HOST']}:{cfg['PUBLIC_GPS_PORT']}",
        "bind": f"{cfg['RECEIVER_BIND_HOST']}:{cfg['RECEIVER_PORT']}",
        "poll_ms": cfg["TERMINAL_POLL_MS"], "port_log": cfg["PORT_LOG"],
        "retention_h": cfg["PORT_LOG_RETENTION_HOURS"], "auto_register": cfg["AUTO_REGISTER_UNKNOWN"],
    })


@login_required
def live(request):
    return render(request, "tracking/live.html", {"nav": "live"})


@login_required
def history(request):
    devices = Device.objects.all()
    return render(request, "tracking/history.html", {"nav": "history", "devices": devices})


@login_required
def records(request):
    qs = AVLRecord.objects.select_related("device").annotate(io_count=Count("io")).order_by("-timestamp_utc", "-id")
    dev = request.GET.get("device")
    if dev:
        qs = qs.filter(device_id=dev)
    if request.GET.get("fix") == "valid":
        qs = qs.filter(valid_fix=True)
    elif request.GET.get("fix") == "invalid":
        qs = qs.filter(valid_fix=False)
    if request.GET.get("late"):
        qs = qs.filter(late=True)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "tracking/records.html", {"nav": "records", "page": page,
                                                     "devices": Device.objects.all(), "q": request.GET})


@login_required
def record_detail(request, pk):
    rec = get_object_or_404(AVLRecord.objects.select_related("device", "packet"), pk=pk)
    return render(request, "tracking/io_inspector.html", {
        "nav": "records", "rec": rec, "io": rec.io.all(),
        "mapping_version": io_dictionary.MAPPING_VERSION,
    })


@login_required
def io_inspector(request):
    """Latest record per device with all I/O, or a specific record."""
    rec = AVLRecord.objects.select_related("device", "packet")
    dev = request.GET.get("device")
    if dev:
        rec = rec.filter(device_id=dev)
    rec = rec.order_by("-received_utc", "-id").first()
    if rec:
        return redirect("record_detail", pk=rec.pk)
    messages.info(request, "No decoded records yet. Send data with the simulator or a device first.")
    return redirect("records")


@login_required
@raw_required
def raw_packets(request):
    qs = RawPacket.objects.select_related("device", "connection")
    q = request.GET
    if q.get("device"):
        qs = qs.filter(device_id=q["device"])
    if q.get("status") == "error":
        qs = qs.filter(parse_status="error")
    elif q.get("status") == "ok":
        qs = qs.filter(parse_status="ok")
    if q.get("codec"):
        qs = qs.filter(codec=q["codec"])
    if q.get("search"):
        qs = qs.filter(Q(hex__icontains=q["search"]) | Q(error__icontains=q["search"]))
    if q.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="raw_packets.csv"'
        w = csv.writer(resp)
        w.writerow(["received_utc", "imei_masked", "source", "codec", "size", "crc_ok", "records", "new",
                    "duplicates", "parse_status", "error", "ack_status", "ack_value", "hex"])
        for p in qs[:5000]:
            w.writerow([p.received_utc.isoformat(), p.device.masked_imei if p.device else "",
                        p.connection.source if p.connection else "", p.codec, p.size, p.crc_ok, p.record_count,
                        p.new_records, p.duplicate_records, p.parse_status, p.error, p.ack_status, p.ack_value, p.hex])
        return resp
    page = Paginator(qs, 50).get_page(q.get("page"))
    return render(request, "tracking/raw.html", {"nav": "raw", "page": page, "devices": Device.objects.all(), "q": q})


@login_required
@raw_required
def raw_detail(request, pk):
    p = get_object_or_404(RawPacket, pk=pk)
    decoded, err = None, ""
    if not p.truncated:
        try:
            decoded = codec.decode_frame(bytes.fromhex(p.hex))
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
    hexs = p.hex
    parts = []
    if len(hexs) >= 24:
        parts = [("Preamble", hexs[:8]), ("Data length", hexs[8:16]), ("Codec", hexs[16:18]),
                 ("Record count", hexs[18:20]), ("Records … count 2", hexs[20:-8]), ("CRC", hexs[-8:])]
    return render(request, "tracking/raw_detail.html", {"nav": "raw", "p": p, "decoded": decoded,
                                                        "decode_error": err, "parts": parts,
                                                        "records": p.records.all()})


class DeviceForm(forms.ModelForm):
    class Meta:
        model = Device
        fields = ["imei", "name", "plate", "group", "model", "firmware", "config_revision", "enabled", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def clean_imei(self):
        imei = self.cleaned_data["imei"].strip()
        if not imei.isdigit() or not 14 <= len(imei) <= 17:
            raise forms.ValidationError("IMEI must be 14-17 digits (normally 15).")
        return imei


@login_required
def devices(request):
    form = DeviceForm(request.POST or None)
    if request.method == "POST":
        if not request.user.is_staff:
            messages.error(request, "Only staff users can register devices.")
        elif form.is_valid():
            d = form.save()
            messages.success(request, f"Device {d.name} registered. The receiver will now accept IMEI {d.masked_imei}.")
            return redirect("devices")
    now = timezone.now()
    rows = [device_status(d, now) | {"obj": d} for d in Device.objects.all()]
    return render(request, "tracking/devices.html", {"nav": "devices", "rows": rows, "form": form})


@login_required
def device_detail(request, pk):
    d = get_object_or_404(Device, pk=pk)
    if request.method == "POST":
        if not request.user.is_staff:
            messages.error(request, "Only staff users can edit devices.")
            return redirect("device_detail", pk=pk)
        form = DeviceForm(request.POST, instance=d)
        if form.is_valid():
            form.save()
            messages.success(request, "Device updated.")
            return redirect("device_detail", pk=pk)
    else:
        form = DeviceForm(instance=d)
    return render(request, "tracking/device_detail.html", {
        "nav": "devices", "d": d, "s": device_status(d), "form": form,
        "connections": d.connections.all()[:25],
        "errors": d.packets.filter(parse_status="error")[:15],
        "full_imei": request.user.is_staff,
    })


@login_required
def connectivity(request):
    cfg = settings.FLEET
    rejected = Connection.objects.filter(handshake__in=["rejected", "invalid"])[:20]
    return render(request, "tracking/connectivity.html", {
        "nav": "connectivity", "receiver": receiver_status(), "runs": TestRun.objects.all()[:10],
        "recent": Connection.objects.select_related("device")[:20], "rejected": rejected,
        "selftest_imei_registered": Device.objects.filter(imei=cfg["SELFTEST_IMEI"], enabled=True).exists(),
        "selftest_imei_masked": mask_imei(cfg["SELFTEST_IMEI"]),
    })


@login_required
@require_POST
def run_selftest(request):
    if not request.user.is_staff:
        messages.error(request, "Only staff users can run the connectivity self-test.")
        return redirect("connectivity")
    from .simulator import run_connectivity_suite
    cfg = settings.FLEET
    Device.objects.get_or_create(imei=cfg["SELFTEST_IMEI"],
                                 defaults={"name": "Self-test simulator", "model": "SIMULATOR", "group": "Test"})
    host = request.POST.get("host") or cfg["SELFTEST_HOST"]
    port = int(request.POST.get("port") or cfg["SELFTEST_PORT"])
    full = request.POST.get("mode", "full") == "full"
    steps = run_connectivity_suite(host, port, cfg["SELFTEST_IMEI"], full=full, timeout=8)
    ok = all(s["ok"] for s in steps)
    passed = sum(s["ok"] for s in steps)
    run = TestRun.objects.create(target=f"{host}:{port}", passed=ok, steps=steps,
                                 summary=f"{passed}/{len(steps)} steps passed")
    (messages.success if ok else messages.error)(request, f"Self-test against {host}:{port}: {run.summary}")
    return redirect("connectivity")


@login_required
def settings_page(request):
    from django.db import connection as dbc
    return render(request, "tracking/settings.html", {
        "nav": "settings", "db_vendor": dbc.vendor, "db_engine": settings.DB_ENGINE,
        "mapping_version": io_dictionary.MAPPING_VERSION,
        "io_map": sorted((k, v[0], v[3]) for k, v in io_dictionary.IO_MAP.items()),
        "tz": settings.TIME_ZONE,
    })


# ---------------------------------------------------------------------------
# JSON API (polled every 5 s by the UI)
# ---------------------------------------------------------------------------
@login_required
def api_fleet(request):
    now = timezone.now()
    data = [device_status(d, now) for d in Device.objects.all()]
    return JsonResponse({"server_time": now.isoformat(), "devices": data, "receiver": receiver_status()})


@login_required
def api_history(request):
    dev = get_object_or_404(Device, pk=request.GET.get("device"))
    now = timezone.now()
    start = parse_datetime(request.GET.get("from") or "") or now - timedelta(hours=24)
    end = parse_datetime(request.GET.get("to") or "") or now
    if timezone.is_naive(start):
        start = timezone.make_aware(start)
    if timezone.is_naive(end):
        end = timezone.make_aware(end)
    qs = dev.records.filter(timestamp_utc__gte=start, timestamp_utc__lte=end).order_by("timestamp_utc", "id")[:20000]
    pts = [{
        "id": r.id, "ts": r.timestamp_utc.isoformat(), "rx": r.received_utc.isoformat(),
        "lat": r.latitude, "lon": r.longitude, "speed": r.speed, "angle": r.angle, "alt": r.altitude,
        "sats": r.satellites, "valid": r.valid_fix, "late": r.late, "event": r.event_io_id,
    } for r in qs]
    valid = [p for p in pts if p["valid"]]
    return JsonResponse({"device": dev.name, "from": start.isoformat(), "to": end.isoformat(),
                         "count": len(pts), "valid": len(valid), "late": sum(p["late"] for p in pts),
                         "max_speed": max((p["speed"] for p in valid), default=0), "points": pts})


@login_required
def api_receiver(request):
    return JsonResponse(receiver_status())
