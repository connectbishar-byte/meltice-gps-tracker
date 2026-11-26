"""
SSH-friendly live view of everything arriving on the tracker port (same data as the web Port terminal).

    python manage.py port_monitor                 # last 50 events, then follow live (Ctrl+C to stop)
    python manage.py port_monitor --imei 3563...  # one device
    python manage.py port_monitor --kind rx,tx    # only the bytes
    python manage.py port_monitor --no-follow -n 500 > capture.log
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from tracking.models import PortEvent

LABEL = {"connect": "CONN", "refused": "REFUSED", "rx": "RX", "tx": "TX", "imei": "IMEI", "frame": "FRAME",
         "error": "ERROR", "close": "CLOSE", "info": "INFO"}
COLOR = {"rx": "36", "tx": "35", "ok": "32", "warn": "33", "error": "31", "dim": "90"}


class Command(BaseCommand):
    help = "Print the tracker port event stream (connections, RX/TX HEX, decoded frames, ACKs, errors) and follow it."

    def add_arguments(self, parser):
        parser.add_argument("-n", "--lines", type=int, default=50, help="How many past events to show first")
        parser.add_argument("--no-follow", action="store_true", help="Print and exit")
        parser.add_argument("--imei", default="", help="Only this IMEI (partial match)")
        parser.add_argument("--remote", default="", help="Only this IP / IP:port (partial match)")
        parser.add_argument("--kind", default="", help="Comma list: connect,refused,rx,tx,imei,frame,error,close")
        parser.add_argument("--interval", type=float, default=0.5, help="Polling interval in seconds")
        parser.add_argument("--spaced", action="store_true", help="Print HEX with a space between bytes")

    def _qs(self, o):
        qs = PortEvent.objects.all()
        if o["imei"]:
            qs = qs.filter(imei__contains=o["imei"])
        if o["remote"]:
            qs = qs.filter(remote__contains=o["remote"])
        if o["kind"]:
            qs = qs.filter(kind__in=[k.strip() for k in o["kind"].split(",") if k.strip()])
        return qs

    def _fmt(self, e, o):
        color = not o["no_color"] and self.stdout.isatty()
        c = (lambda code, s: f"\033[{code}m{s}\033[0m") if color else (lambda code, s: s)
        t = timezone.localtime(e.ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        kind_color = COLOR.get(e.kind) if e.kind in ("rx", "tx") else COLOR.get(e.level, "0")
        head = f"{c(COLOR['dim'], t)}  {e.remote:<21}  {(e.imei or '-'):<15}  {c(kind_color, f'{LABEL.get(e.kind, e.kind):<7}')}"
        if e.kind in ("rx", "tx"):
            hx = " ".join(e.hex[i:i + 2] for i in range(0, len(e.hex), 2)) if o["spaced"] else e.hex
            extra = f" ({e.message})" if e.message else ""
            return f"{head}  {e.size} B{extra}  {hx}{' …(truncated)' if e.truncated else ''}"
        msg = e.message + (f"  [packet #{e.packet_id}]" if e.packet_id else "")
        return f"{head}  {c(kind_color, msg) if e.level != 'info' else msg}"

    def handle(self, *args, **o):
        cfg = settings.FLEET
        if not cfg["PORT_LOG"]:
            self.stderr.write("FLEET_PORT_LOG is off: the receiver is not recording port events.")
        rows = list(self._qs(o).order_by("-id")[: o["lines"]])[::-1]
        for e in rows:
            self.stdout.write(self._fmt(e, o))
        if o["no_follow"]:
            return
        last = rows[-1].id if rows else (PortEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0)
        self.stdout.write(f"--- following port {cfg['RECEIVER_PORT']} events (Ctrl+C to stop) ---")
        try:
            while True:
                close_old_connections()
                for e in self._qs(o).filter(id__gt=last).order_by("id")[:1000]:
                    self.stdout.write(self._fmt(e, o))
                    last = e.id
                self.stdout.flush()
                time.sleep(o["interval"])
        except KeyboardInterrupt:
            self.stdout.write("")
