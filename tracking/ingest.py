"""
Synchronous persistence pipeline used by the asyncio receiver (run in a worker thread).

Rule from the scope: raw frame + decoded records are committed in ONE transaction and the
binary ACK is only sent after that commit. Replays are idempotent via (device, fingerprint).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_tz

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import codec, io_dictionary as iod
from .models import AVLRecord, Connection, Device, IOValue, PortEvent, RawPacket

log = logging.getLogger("fleet.ingest")


@dataclass
class IngestResult:
    packet_id: int | None
    ack: int | None          # value to send, None = send nothing
    records: int = 0
    new: int = 0
    duplicates: int = 0
    error: str = ""
    summary: str = ""        # one-line human description for the port terminal


def lookup_device(imei: str) -> Device | None:
    device = Device.objects.filter(imei=imei).first()
    if device is None and settings.FLEET.get("AUTO_REGISTER_UNKNOWN") and imei.isdigit() and 14 <= len(imei) <= 17:
        # Test/commissioning mode (FLEET_AUTO_REGISTER_UNKNOWN=true): accept any well-formed IMEI so its data
        # can be inspected before anyone registers it. Never enable this on an internet-facing port long term.
        device = Device.objects.create(imei=imei, name=f"Auto-registered {imei[-4:]}", group="Auto-registered",
                                       notes="Created automatically by the receiver (FLEET_AUTO_REGISTER_UNKNOWN).")
        log.warning("auto-registered unknown IMEI %s", imei)
    return device if device and device.enabled else None


def open_connection(remote: str, imei: str, source: str = "tcp") -> tuple[int, int | None, bool]:
    """Record a connection attempt. Returns (connection_id, device_id, accepted)."""
    device = lookup_device(imei)
    conn = Connection.objects.create(
        device=device, claimed_imei=imei, remote=remote, source=source,
        handshake="accepted" if device else "rejected",
    )
    if device:
        Device.objects.filter(pk=device.pk).update(last_connection_utc=timezone.now())
    return conn.id, device.id if device else None, device is not None


def log_invalid_handshake(remote: str, reason: str, source: str = "tcp") -> None:
    Connection.objects.create(remote=remote, source=source, handshake="invalid",
                              closed_utc=timezone.now(), close_reason=reason[:200])


def close_connection(conn_id: int, reason: str) -> None:
    Connection.objects.filter(pk=conn_id).update(closed_utc=timezone.now(), close_reason=reason[:200])


def _ts(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=dt_tz.utc)


def _store_hex(frame: bytes) -> tuple[str, bool]:
    limit = settings.FLEET["RAW_HEX_STORE_LIMIT"]
    if len(frame) > limit:
        return frame[:limit].hex().upper(), True
    return frame.hex().upper(), False


def store_bad_frame(conn_id: int | None, device_id: int | None, frame: bytes, error: str) -> int:
    """Persist an invalid / unparsable frame (bounded) for audit. Never raises."""
    try:
        hx, trunc = _store_hex(frame)
        pkt = RawPacket.objects.create(
            connection_id=conn_id, device_id=device_id, hex=hx, size=len(frame), truncated=trunc,
            crc_ok=False if "CRC" in error else None, parse_status="error", error=error[:300],
            ack_status="pending", sha256=hashlib.sha256(frame).hexdigest(),
        )
        if device_id:
            Device.objects.filter(pk=device_id).update(
                error_count=F("error_count") + 1, last_error=error[:300], last_packet_utc=timezone.now())
        return pkt.id
    except Exception:  # pragma: no cover - audit must not crash the receiver
        log.exception("could not store bad frame")
        return 0


def ingest_frame(conn_id: int, device_id: int, imei: str, frame: bytes) -> IngestResult:
    """
    Decode and persist one complete frame. Returns the ACK to send.
      - protocol/CRC error -> raw packet stored with error, ACK 0 (device will resend)
      - success            -> raw + records committed, ACK = number of records in frame
      - DB failure         -> exception propagates; caller must NOT ACK
    """
    try:
        pkt = codec.decode_frame(frame)
    except codec.ProtocolError as exc:
        pid = store_bad_frame(conn_id, device_id, frame, str(exc))
        return IngestResult(pid, ack=0, error=str(exc))

    fault = settings.FLEET.get("FAULT_INJECT_IMEIS") or set()
    now = timezone.now()
    hx, trunc = _store_hex(frame)

    with transaction.atomic():
        device = Device.objects.select_for_update().get(pk=device_id)
        raw = RawPacket.objects.create(
            connection_id=conn_id, device=device, received_utc=now, hex=hx, size=len(frame), truncated=trunc,
            crc_ok=True, crc_value=f"{pkt.crc & 0xFFFF:04X}", codec=pkt.codec_name,
            record_count=len(pkt.records), parse_status="ok", ack_status="pending",
            sha256=hashlib.sha256(frame).hexdigest(),
        )
        new = dup = 0
        newest_ts = device.last_record_ts
        for idx, rec in enumerate(pkt.records):
            fp = rec.fingerprint(imei)
            if AVLRecord.objects.filter(device=device, fingerprint=fp).exists():
                dup += 1
                continue
            ts = _ts(rec.timestamp_ms)
            late = bool(device.last_record_ts and ts < device.last_record_ts)
            valid = rec.has_valid_fix
            ar = AVLRecord.objects.create(
                device=device, packet=raw, index=idx, timestamp_utc=ts, received_utc=now,
                latitude=rec.latitude, longitude=rec.longitude, altitude=rec.altitude, angle=rec.angle,
                satellites=rec.satellites, speed=rec.speed, priority=rec.priority,
                event_io_id=rec.event_io_id, valid_fix=valid, late=late, fingerprint=fp,
            )
            io_rows = []
            io_by_id = {}
            for e in rec.io:
                info = iod.interpret(e.avl_id, e.raw)
                io_by_id[e.avl_id] = info["value"]
                io_rows.append(IOValue(
                    record=ar, avl_id=e.avl_id, byte_length=e.length, raw_hex=e.raw.hex().upper()[:2048],
                    value=info["value"] if isinstance(info["value"], (int, float)) and abs(info["value"]) < 1e15 else None,
                    text=str(info["text"])[:200], name=info["name"][:80], unit=info["unit"], known=info["known"],
                    mapping_version=iod.MAPPING_VERSION,
                ))
            IOValue.objects.bulk_create(io_rows)
            new += 1

            # status only moves forward in time; a delayed record never overwrites a newer state
            if newest_ts is None or ts >= newest_ts:
                newest_ts = ts
                if iod.IGNITION in io_by_id:
                    device.last_ignition = bool(io_by_id[iod.IGNITION])
                if iod.MOVEMENT in io_by_id:
                    device.last_movement = bool(io_by_id[iod.MOVEMENT])
                if iod.GSM_SIGNAL in io_by_id:
                    device.last_gsm_signal = int(io_by_id[iod.GSM_SIGNAL])
                if iod.EXT_VOLTAGE in io_by_id:
                    device.last_ext_voltage = io_by_id[iod.EXT_VOLTAGE]
            if valid and (device.last_fix_ts is None or ts > device.last_fix_ts):
                device.last_fix_ts = ts
                device.last_fix_lat = rec.latitude
                device.last_fix_lon = rec.longitude
                device.last_fix_speed = rec.speed
                device.last_fix_angle = rec.angle
                device.last_fix_sats = rec.satellites

        raw.new_records, raw.duplicate_records = new, dup
        raw.save(update_fields=["new_records", "duplicate_records"])
        device.last_record_ts = newest_ts
        device.last_packet_utc = now
        device.packet_count += 1
        device.record_count += new
        device.save()
        Connection.objects.filter(pk=conn_id).update(frames=F("frames") + 1)

        if imei in fault:
            # Test hook (FLEET_FAULT_INJECT_IMEIS): simulate a DB failure after writes -> rollback, no ACK.
            raise RuntimeError(f"fault injection: simulated database failure for {imei}")

    summary = (f"Codec {pkt.codec_name} · {len(pkt.records)} record(s), {new} new, {dup} duplicate · "
               f"CRC {pkt.crc & 0xFFFF:04X} OK")
    if pkt.records:
        last = max(pkt.records, key=lambda r: r.timestamp_ms)
        pos = (f"{last.latitude:.6f}, {last.longitude:.6f}" if last.has_valid_fix else "no GNSS fix")
        summary += (f" · latest {_ts(last.timestamp_ms):%Y-%m-%d %H:%M:%S} UTC · {pos} · {last.speed} km/h · "
                    f"{last.satellites} sats · {len(last.io)} I/O")
    return IngestResult(raw.id, ack=len(pkt.records), records=len(pkt.records), new=new, duplicates=dup,
                        summary=summary)


def mark_ack(packet_id: int | None, status: str, value: int | None) -> None:
    if packet_id:
        RawPacket.objects.filter(pk=packet_id).update(ack_status=status, ack_value=value)


# ---------------------------------------------------------------------------
# Port terminal events
# ---------------------------------------------------------------------------
def write_event(ev: dict) -> None:
    """Persist one PortEvent. `data` (bytes) is stored as bounded HEX. Never raises."""
    try:
        data = ev.pop("data", b"") or b""
        limit = settings.FLEET["RAW_HEX_STORE_LIMIT"] // 2
        PortEvent.objects.create(size=ev.pop("size", len(data)), hex=data[:limit].hex().upper(),
                                 truncated=len(data) > limit, **ev)
    except Exception:  # pragma: no cover - logging must never break the receiver
        log.exception("could not store port event")


def prune_events(hours: int) -> int:
    if hours <= 0:
        return 0
    cutoff = timezone.now() - timedelta(hours=hours)
    deleted, _ = PortEvent.objects.filter(ts__lt=cutoff).delete()
    return deleted
