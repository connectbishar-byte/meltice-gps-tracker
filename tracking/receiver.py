"""
Asyncio TCP receiver for Teltonika devices (Codec 8 / 8E).

Independent of the web process. Run with:
    python manage.py run_receiver --host 0.0.0.0 --port 5027
"""
from __future__ import annotations

import asyncio
import collections
import ipaddress
import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import codec, ingest

log = logging.getLogger("fleet.receiver")


class Receiver:
    def __init__(self, host: str, port: int, name: str = "receiver"):
        cfg = settings.FLEET
        self.host, self.port, self.name = host, port, name
        self.max_frame = cfg["MAX_FRAME_BYTES"]
        self.max_connections = cfg["MAX_CONNECTIONS"]
        self.handshake_timeout = cfg["HANDSHAKE_TIMEOUT"]
        self.read_timeout = cfg["READ_TIMEOUT"]
        self.rate_limit = cfg["CONNECTS_PER_MINUTE_PER_IP"]
        # One DB worker thread serialises writes (safe for SQLite, fine for a single-device trial).
        self.db = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fleet-db")
        self.active = 0
        self.total = 0
        self.frames_ok = 0
        self.frames_bad = 0
        self.started = timezone.now()
        self._recent: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.server: asyncio.base_events.Server | None = None
        self.port_log = cfg.get("PORT_LOG", True)
        self.event_retention_hours = cfg.get("PORT_LOG_RETENTION_HOURS", 72)

    # -- helpers ---------------------------------------------------------
    async def run_db(self, fn, *args):
        def job():
            close_old_connections()
            return fn(*args)
        return await asyncio.get_running_loop().run_in_executor(self.db, job)

    def event(self, kind: str, remote: str, *, level: str = "info", imei: str = "", conn_id=None,
              packet_id=None, data: bytes = b"", message: str = "", size: int | None = None) -> None:
        """Queue a port-terminal event. Fire-and-forget on the single DB thread, so order is preserved
        and an event never delays an ACK beyond the queue position it was written in."""
        if not self.port_log:
            return
        ev = dict(ts=timezone.now(), kind=kind, level=level, remote=remote, imei=imei or "",
                  connection_id=conn_id, packet_id=packet_id or None, data=bytes(data), message=message[:300])
        if size is not None:
            ev["size"] = size

        def job():
            close_old_connections()
            ingest.write_event(ev)
        asyncio.get_running_loop().run_in_executor(self.db, job)

    async def send(self, writer, data: bytes, remote: str, imei: str, conn_id, message: str):
        writer.write(data)
        await writer.drain()
        self.event("tx", remote, imei=imei, conn_id=conn_id, data=data, message=message)

    def _rate_limited(self, ip: str) -> bool:
        now = time.monotonic()
        dq = self._recent[ip]
        while dq and now - dq[0] > 60:
            dq.popleft()
        dq.append(now)
        return len(dq) > self.rate_limit

    @staticmethod
    def _source(ip: str) -> str:
        try:
            return "loopback" if ipaddress.ip_address(ip).is_loopback else "remote"
        except ValueError:
            return "remote"

    # -- lifecycle -------------------------------------------------------
    async def start(self):
        self.server = await asyncio.start_server(self.handle, self.host, self.port, reuse_address=True)
        addrs = ", ".join(str(s.getsockname()) for s in self.server.sockets)
        log.info("Teltonika receiver listening on %s", addrs)
        return self.server

    async def serve_forever(self):
        await self.start()
        beat = asyncio.create_task(self.heartbeat_loop())
        try:
            async with self.server:
                await self.server.serve_forever()
        finally:
            beat.cancel()

    async def heartbeat_loop(self):
        from .models import ReceiverHeartbeat

        def beat():
            ReceiverHeartbeat.objects.update_or_create(
                name=self.name,
                defaults=dict(host=self.host, port=self.port, started_utc=self.started, last_beat_utc=timezone.now(),
                              active_connections=self.active, total_connections=self.total,
                              frames_ok=self.frames_ok, frames_bad=self.frames_bad, pid=os.getpid()),
            )
        loops = 0
        while True:
            try:
                await self.run_db(beat)
                if loops % 120 == 0:  # every ~10 minutes
                    n = await self.run_db(ingest.prune_events, self.event_retention_hours)
                    if n:
                        log.info("pruned %d port events older than %d h", n, self.event_retention_hours)
            except Exception:
                log.exception("heartbeat failed")
            loops += 1
            await asyncio.sleep(5)

    # -- connection handler ----------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = str(peer[0])
        remote = f"{ip}:{peer[1]}"
        source = self._source(ip)
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass

        if self.active >= self.max_connections or self._rate_limited(ip):
            log.warning("refusing %s (limit reached)", remote)
            self.event("refused", remote, level="warn",
                       message="Connection refused: connection limit or per-IP rate limit reached")
            writer.close()
            return
        self.active += 1
        self.total += 1
        conn_id = None
        imei = ""
        frames = 0
        opened = time.monotonic()
        reason = "closed"
        self.event("connect", remote, message=f"TCP connection opened ({source}) · {self.active} active")
        try:
            # ---- handshake: 2-byte length + IMEI
            buf = bytearray()
            imei = None
            deadline = time.monotonic() + self.handshake_timeout
            while imei is None:
                try:
                    imei, used = codec.try_parse_imei(bytes(buf))
                    del buf[:used]
                except codec.NeedMoreData:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    chunk = await asyncio.wait_for(reader.read(4096), remaining)
                    if not chunk:
                        reason = "closed before sending an IMEI"
                        await self.run_db(ingest.log_invalid_handshake, remote, "closed before IMEI", source)
                        return
                    self.event("rx", remote, data=chunk, message="handshake bytes")
                    buf += chunk
                    if len(buf) > 64:
                        raise codec.ProtocolError("oversized handshake")

            conn_id, device_id, accepted = await self.run_db(ingest.open_connection, remote, imei, source)
            if not accepted:
                log.warning("rejected unknown/disabled IMEI %s from %s", imei, remote)
                self.event("imei", remote, level="warn", imei=imei, conn_id=conn_id,
                           message=f"IMEI {imei} is not registered or is disabled: rejecting")
                await self.send(writer, b"\x00", remote, imei, conn_id, "handshake reply 00 (rejected)")
                reason = "IMEI not registered or disabled"
                return
            self.event("imei", remote, level="ok", imei=imei, conn_id=conn_id, message=f"IMEI {imei} accepted")
            await self.send(writer, b"\x01", remote, imei, conn_id, "handshake reply 01 (accepted)")
            log.info("accepted IMEI %s from %s", imei, remote)

            # ---- AVL frames (handles fragmented and coalesced TCP reads)
            while True:
                try:
                    frame, used = codec.extract_frame(bytes(buf), self.max_frame)
                except codec.NeedMoreData:
                    chunk = await asyncio.wait_for(reader.read(65536), self.read_timeout)
                    if not chunk:
                        reason = "device closed connection"
                        return
                    self.event("rx", remote, imei=imei, conn_id=conn_id, data=chunk)
                    buf += chunk
                    continue
                except codec.ProtocolError as exc:
                    # framing is lost: audit what we have and drop the link
                    self.frames_bad += 1
                    pid = await self.run_db(ingest.store_bad_frame, conn_id, device_id, bytes(buf[: self.max_frame]), str(exc))
                    self.event("error", remote, level="error", imei=imei, conn_id=conn_id, packet_id=pid,
                               message=f"Framing error, dropping connection: {exc}")
                    reason = f"framing error: {exc}"
                    return
                del buf[:used]

                try:
                    result = await self.run_db(ingest.ingest_frame, conn_id, device_id, imei, frame)
                except Exception as exc:
                    # DB failure -> NO acknowledgement; device keeps the data and resends later
                    self.frames_bad += 1
                    log.error("ingest failed, not acknowledging: %s", exc)
                    pid = await self.run_db(ingest.store_bad_frame, conn_id, device_id, frame, f"DB/ingest failure, not ACKed: {exc}")
                    self.event("error", remote, level="error", imei=imei, conn_id=conn_id, packet_id=pid,
                               message=f"Database/ingest failure, NOT acknowledged (device will resend): {exc}")
                    reason = f"ingest failure: {exc}"
                    return
                frames += 1
                if result.error:
                    self.event("frame", remote, level="error", imei=imei, conn_id=conn_id, packet_id=result.packet_id,
                               size=len(frame), message=f"Invalid frame: {result.error}")
                else:
                    self.event("frame", remote, level="ok", imei=imei, conn_id=conn_id, packet_id=result.packet_id,
                               size=len(frame), message=result.summary)
                await self.send(writer, codec.encode_ack(result.ack), remote, imei, conn_id,
                                f"ACK {result.ack}" + (" (rejected, device will resend)" if result.error else ""))
                if result.error:
                    self.frames_bad += 1
                    await self.run_db(ingest.mark_ack, result.packet_id, "nack", result.ack)
                    log.warning("bad frame from %s: %s (ACK 0)", imei, result.error)
                else:
                    self.frames_ok += 1
                    await self.run_db(ingest.mark_ack, result.packet_id, "sent", result.ack)
                    log.info("IMEI %s: %d records (%d new, %d dup) ACK %d",
                             imei, result.records, result.new, result.duplicates, result.ack)
        except asyncio.TimeoutError:
            reason = "read timeout" if conn_id is not None else "no IMEI received within the handshake timeout"
            if conn_id is None:
                await self.run_db(ingest.log_invalid_handshake, remote, "handshake timeout", source)
        except codec.ProtocolError as exc:
            reason = f"protocol error: {exc}"
            hint = ""
            if conn_id is None and buf:
                head = bytes(buf[:40])
                if all(32 <= b < 127 or b in (9, 10, 13) for b in head):
                    text = head.decode("ascii", "replace").splitlines()[0][:40] if head.strip() else ""
                    hint = f" · data is plain text (\"{text}\"): a browser, scanner or wrong device protocol, not a Teltonika tracker"
            self.event("error", remote, level="error", imei=imei or "", conn_id=conn_id,
                       message=f"Not a valid Teltonika handshake/frame: {exc}{hint}")
            if conn_id is None:
                await self.run_db(ingest.log_invalid_handshake, remote, str(exc), source)
        except (ConnectionError, OSError) as exc:
            reason = f"socket error: {exc}"
        finally:
            self.active -= 1
            secs = time.monotonic() - opened
            self.event("close", remote, level="warn" if reason not in ("device closed connection",) else "info",
                       imei=imei, conn_id=conn_id,
                       message=f"Connection closed: {reason} · {secs:.1f} s · {frames} frame(s) · {self.active} active")
            if conn_id is not None:
                try:
                    await self.run_db(ingest.close_connection, conn_id, reason)
                except Exception:
                    log.exception("could not close connection row")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
