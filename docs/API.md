# REST API v1

Base URL: `https://fleet.melticetech.com/api/v1/` (local: `http://127.0.0.1:8000/api/v1/`).
Interactive documentation with "Try it out": `/api/docs/` (Swagger UI) and `/api/redoc/`. The OpenAPI
schema is at `/api/schema/`. Docs and schema need a signed-in user.

All responses are JSON and all times are ISO 8601. Stored times are UTC; the server returns them in UAE time
(`+04:00`).

## Authentication

Use the same user accounts as the web UI.

| Client | How |
|---|---|
| Browser on the same site | session cookie after signing in at `/login/` (send `X-CSRFToken` on POST/PUT/DELETE) |
| Scripts, Postman, other systems | token: `Authorization: Token <key>` |

```bash
# get a token (10 attempts per minute per client)
curl -s -X POST https://fleet.melticetech.com/api/v1/auth/token/ \
     -H 'Content-Type: application/json' -d '{"username":"admin","password":"..."}'
# {"token": "9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b", "user": {"username": "admin", "is_staff": true, ...}}

curl -H "Authorization: Token 9944b0..." https://fleet.melticetech.com/api/v1/auth/me/
curl -X DELETE -H "Authorization: Token 9944b0..." https://fleet.melticetech.com/api/v1/auth/token/   # revoke
```

A user has one token until it is revoked. Signing out of the web UI does not revoke it.

| Role | Can |
|---|---|
| any signed-in user | read devices (IMEI masked), records, history, connections, receiver status, test results |
| raw-data viewer (`raw_data` group) and staff | also raw packets and port events (full IMEI) |
| staff | also create / edit / delete devices, run connectivity tests |

`401` means no or invalid credentials. `403` means you are signed in but your role does not allow the request.

## Endpoints

| Method and path | Description | Query parameters |
|---|---|---|
| `POST /auth/token/` | exchange username and password for a token | |
| `DELETE /auth/token/` | revoke your token | |
| `GET /auth/me/` | current user and roles | |
| `GET /devices/` | all devices with live status (state, link, last fix, ignition, GSM, voltage) | |
| `POST /devices/` | register a device (staff): `{"imei": "356307042441013", "name": "Truck 7", "plate": "", "group": "", "enabled": true}` | |
| `GET/PUT/PATCH/DELETE /devices/{id}/` | one device | |
| `GET /devices/{id}/history/` | route points, oldest first (max 20,000) | `from`, `to` (ISO 8601; default last 24 h), `valid_only` |
| `GET /records/` | decoded AVL records, newest first, paginated | `device`, `from`, `to`, `valid_fix`, `late`, `limit`, `offset` |
| `GET /records/{id}/` | one record with every I/O element (AVL id, raw HEX, value, unit) | |
| `GET /packets/` | raw frames, paginated (raw-data role) | `device`, `status` (`ok`/`error`), `codec`, `search`, `limit`, `offset` |
| `GET /packets/{id}/` | one frame plus a fresh decode of its HEX | |
| `GET /connections/` | TCP connections including rejected / invalid handshakes | `handshake`, `limit`, `offset` |
| `GET /port-events/` | the port terminal stream (raw-data role), see below | `after`, `before`, `tail`, `limit`, `kind`, `imei`, `remote`, `q` |
| `GET /receiver/` | receiver heartbeat: up, active connections, frames ok/bad, public endpoint | |
| `GET /tests/` | connectivity self-test results | |
| `POST /tests/run/` | run the 13-step protocol self-test (staff): `{"host": "127.0.0.1", "port": 5027, "mode": "full"}` | |

Paginated lists return `{"count", "next", "previous", "results"}` (default 50 per page, `?limit=` up to any
size you need).

## Port events (live tail)

```bash
# newest 200 events
curl -H "Authorization: Token $T" "https://fleet.melticetech.com/api/v1/port-events/"
# then poll for anything newer than the last id you saw
curl -H "Authorization: Token $T" "https://fleet.melticetech.com/api/v1/port-events/?after=1532"
# only one tracker's received bytes and decoded frames
curl -H "Authorization: Token $T" "https://fleet.melticetech.com/api/v1/port-events/?imei=356307042441013&kind=rx,frame"
```

```json
{
  "events": [
    {"id": 1533, "ts": "2026-11-26T17:24:20.649+04:00", "kind": "rx", "level": "info",
     "remote": "100.64.12.7:40112", "imei": "356307042441013", "connection": 88, "packet": null,
     "size": 201, "hex": "00000000000000BD8E0200...", "truncated": false, "message": ""},
    {"id": 1534, "ts": "2026-11-26T17:24:20.678+04:00", "kind": "frame", "level": "ok",
     "remote": "100.64.12.7:40112", "imei": "356307042441013", "connection": 88, "packet": 412,
     "size": 201, "hex": "", "truncated": false,
     "message": "Codec 8E · 2 record(s), 2 new, 0 duplicate · CRC 6AA9 OK · latest ... · 24.390528, 54.705608 · 35 km/h · 11 sats · 13 I/O"},
    {"id": 1535, "ts": "2026-11-26T17:24:20.678+04:00", "kind": "tx", "level": "info",
     "remote": "100.64.12.7:40112", "imei": "356307042441013", "connection": 88, "packet": null,
     "size": 4, "hex": "00000002", "truncated": false, "message": "ACK 2"}
  ],
  "last_id": 1535,
  "receiver": {"up": true, "active": 1, "frames_ok": 812, "frames_bad": 3, "...": "..."}
}
```

| `kind` | Meaning |
|---|---|
| `connect` / `close` | TCP connection opened / closed (message has the reason, duration, frame count) |
| `refused` | connection limit or per-IP rate limit reached |
| `rx` | bytes received exactly as they arrived (`hex`) |
| `tx` | bytes sent: handshake reply `01`/`00`, 4-byte ACK |
| `imei` | handshake result (`level` `ok` = accepted, `warn` = rejected) |
| `frame` | decoded frame summary, `packet` = id for `/packets/{id}/` |
| `error` | CRC / framing / protocol / database error, including non-Teltonika traffic |

`level` is `info`, `ok`, `warn` or `error`. Events older than `FLEET_PORT_LOG_RETENTION_HOURS` (72 h) are deleted.

## Python example

```python
import time, requests

BASE = "https://fleet.melticetech.com/api/v1"
token = requests.post(f"{BASE}/auth/token/", json={"username": "admin", "password": "..."}).json()["token"]
s = requests.Session()
s.headers["Authorization"] = f"Token {token}"

for d in s.get(f"{BASE}/devices/").json():
    print(d["name"], d["status"]["state"], d["status"]["fix"])

last = s.get(f"{BASE}/port-events/", params={"tail": 1}).json()["last_id"]
while True:                                   # follow the port like the web terminal
    r = s.get(f"{BASE}/port-events/", params={"after": last}).json()
    for e in r["events"]:
        print(e["ts"], e["remote"], e["kind"].upper(), e["size"], e["hex"] or e["message"])
    last = r["last_id"]
    time.sleep(1)
```
