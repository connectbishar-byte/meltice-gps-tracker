# Hostinger deployment (Option B)

Target: `https://fleet.melticetech.com` for the web UI and `gps.melticetech.com:5027/TCP` for the FMC130.

> **Plan requirement:** a Hostinger **VPS** (KVM 1 or larger, Ubuntu 22.04/24.04). Shared/Cloud/WordPress
> hosting only serves HTTP(S) and cannot accept the tracker's raw TCP connection on port 5027.
> The database is **Microsoft SQL Server**. By default the installer runs SQL Server 2022 **Express**
> (free for production, 10 GB per database) in Docker on the VPS, listening on `127.0.0.1:1433` only.
> It needs about 2 GB of RAM; KVM 1 (4 GB) is enough.

Two ways to run it on the VPS — pick one:

| | B1. Native (systemd) — matches the scope | B2. Docker on the VPS |
|---|---|---|
| Services | `fleet-receiver.service`, `gunicorn-fleet.service` | `web` + `receiver` containers |
| Web bind | `127.0.0.1:9040` behind Nginx | `127.0.0.1:9040` behind Nginx |
| Install | `sudo bash deploy/hostinger/install.sh` | `docker compose -f docker-compose.yml -f deploy/hostinger/docker-compose.hostinger.yml up -d --build` |

---

## 0. Before you change anything (runbook §7)

```bash
ssh root@<VPS-IP>
sudo tar czf /root/nginx-backup-$(date +%F).tgz /etc/nginx     # back up Nginx
sudo ss -ltnp                                                 # inventory listening ports
sudo ss -ltnp | grep -E ":(5027|9040)\b"                      # must print nothing
sudo ufw status verbose
```

Note which other sites run on this VPS — you will re-check them at the end.

## 1. DNS (Hostinger hPanel → Domains → DNS / or Cloudflare)

| Type | Name | Value | Proxy |
|---|---|---|---|
| A | `fleet` | VPS public IPv4 | may be proxied (HTTP) |
| A | `gps` | VPS public IPv4 | **DNS only** (grey cloud) — Cloudflare's HTTP proxy cannot carry Teltonika TCP |

Check from your PC: `nslookup gps.melticetech.com` → VPS IP.

## 2. Upload the code

On the VPS:

```bash
cd /root && git clone https://github.com/connectbishar-byte/meltice-gps-tracker.git && cd meltice-gps-tracker
```

(Private repository: use a GitHub deploy key or a fine-grained personal access token with read access.)

## 3a. Install — native (B1)

```bash
sudo bash deploy/hostinger/install.sh
# other domains:          sudo WEB_DOMAIN=fleet.example.com GPS_DOMAIN=gps.example.com bash deploy/hostinger/install.sh
# existing SQL Server:    sudo DB_MODE=external DB_HOST=10.0.0.5 DB_NAME=meltice_gps_tracker DB_USER=fleet_app \
#                              DB_PASSWORD='...' [DB_ADMIN_USER=sa DB_ADMIN_PASSWORD='...'] bash deploy/hostinger/install.sh
```

The script prints the generated **admin password** — save it. It creates:

* service account `fleet`, code in `/srv/meltice-gps-tracker/app`, virtualenv `/srv/meltice-gps-tracker/venv`
* config `/etc/meltice-gps-tracker/fleet.env` (root:fleet, 640) with a random `FLEET_SECRET_KEY` and app DB password
* Microsoft ODBC Driver 18 for SQL Server
* SQL Server 2022 Express container `meltice-mssql` (`--restart unless-stopped`, `127.0.0.1:1433`), data in
  `/var/opt/meltice-mssql`, `sa` password in `/etc/meltice-gps-tracker/mssql.env` (root only, 600)
* database `meltice_gps_tracker` and login `fleet_app` (db_owner of that database only, no server rights);
  the app never uses `sa`
* logs `/var/log/meltice-gps-tracker/{web,receiver}.log`
* systemd units `gunicorn-fleet` (127.0.0.1:9040) and `fleet-receiver` (0.0.0.0:5027)
* a **new** Nginx site for `fleet.melticetech.com` only (other sites untouched; `nginx -t` before reload)
* the self-test IMEI `356307042441013` on the allow-list (disable it in *Devices* after testing)

It does **not** touch UFW, TLS or the Hostinger panel firewall — do those in step 4.

Re-running is safe (keeps `fleet.env` and data). Upgrades: unpack the new release and run
`sudo bash deploy/hostinger/update.sh` (it backs up first).

## 3b. Install — Docker (B2)

```bash
curl -fsSL https://get.docker.com | sh            # if Docker is not installed (Hostinger also has a Docker VPS template)
cp .env.example .env && nano .env                 # set FLEET_SECRET_KEY, FLEET_ADMIN_PASSWORD, MSSQL_SA_PASSWORD,
                                                  # FLEET_DB_PASSWORD, FLEET_PUBLIC_GPS_HOST=gps.melticetech.com
docker compose -f docker-compose.yml -f deploy/hostinger/docker-compose.hostinger.yml up -d --build
sudo cp deploy/hostinger/nginx-fleet.melticetech.com.conf /etc/nginx/sites-available/fleet.melticetech.com
sudo ln -s /etc/nginx/sites-available/fleet.melticetech.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 4. HTTPS and firewalls

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d fleet.melticetech.com          # obtains cert and adds HTTP→HTTPS redirect

# UFW — only if it is already active. Never enable UFW before allowing SSH.
sudo ufw allow 5027/tcp
sudo ufw allow 'Nginx Full'
```

**hPanel → VPS → Security → Firewall:** if a firewall is attached, add *Accept TCP 5027* (and 80/443).
Where the SIM operator's egress range is known, restrict 5027 to that source range.
Never expose the database port publicly.

## 5. Verify

On the VPS:

```bash
sudo systemctl status fleet-receiver gunicorn-fleet --no-pager
sudo ss -ltnp | grep -E ":(5027|9040)\b"         # 0.0.0.0:5027 python, 127.0.0.1:9040 gunicorn
curl -I http://127.0.0.1:9040/login/              # 200
sudo -u fleet /srv/meltice-gps-tracker/venv/bin/python /srv/meltice-gps-tracker/app/tools/fmc_probe.py 127.0.0.1 5027
sudo journalctl -u fleet-receiver -f              # live receiver log
```

From your PC (different network — this is what proves the tracker can reach it):

```powershell
Test-NetConnection gps.melticetech.com -Port 5027
python tools\fmc_probe.py gps.melticetech.com 5027
```

Then log in at `https://fleet.melticetech.com` and open **Port terminal**: the probe's connections, every byte
it sent, the server's replies and the decoded frames appear there live. **Connectivity** lists the connections
and **Live map** shows the probe's test point. Over SSH, `manage.py port_monitor` shows the same stream
(see README).

Finally re-check every other site/application on the VPS still works (scope: *Regression*).

## 6. Point the FMC130 at Hostinger

In Teltonika Configurator → **GPRS**: Domain `gps.melticetech.com`, Port `5027`, Protocol `TCP`,
ACK `AVL`, TLS `None` (controlled test only). Register the device's IMEI on the **Devices** page first
(or on the VPS: `sudo -u fleet bash -c "set -a; . /etc/meltice-gps-tracker/fleet.env; cd /srv/meltice-gps-tracker/app; /srv/meltice-gps-tracker/venv/bin/python manage.py register_device <IMEI> <Name>"`). Change one destination at a time. See `docs/DEVICE_BENCH_TEST.md`.

Optional packet capture while testing (contains location data — keep it short, delete after):
`sudo tcpdump -ni any tcp port 5027 -c 50`

## 7. Database (SQL Server)

| | Default (`DB_MODE=docker`) | `DB_MODE=external` |
|---|---|---|
| Server | SQL Server 2022 Express in Docker on this VPS | your existing SQL Server 2019/2022 |
| Address | `127.0.0.1:1433` (not reachable from the internet) | `DB_HOST:DB_PORT`, private network / VPN |
| Created by | `install_sqlserver.sh` + `manage.py mssql_setup` | `mssql_setup` when `DB_ADMIN_PASSWORD` is given, else you create DB + login |

Useful commands on the VPS:

```bash
sudo docker logs --tail 50 meltice-mssql                       # SQL Server log
sudo docker exec -it meltice-mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa \
     -P "$(sudo grep MSSQL_SA_PASSWORD /etc/meltice-gps-tracker/mssql.env | cut -d= -f2-)"   # sa console
```

Connect from your PC with SSMS / Azure Data Studio through an SSH tunnel (port 1433 stays closed):
`ssh -L 1433:127.0.0.1:1433 root@<VPS-IP>` then connect to `127.0.0.1,1433`, login `fleet_app`
(password in `/etc/meltice-gps-tracker/fleet.env`), *Trust server certificate*.

Never open 1433 in UFW or the hPanel firewall. `FLEET_DB_EXTRA=TrustServerCertificate=yes` is right for the
local Docker server; for a remote server with a proper certificate use `Encrypt=yes`.

Quick trial without SQL Server: `sudo DB_MODE=sqlite bash deploy/hostinger/install.sh`.

## 8. Operations

* Backups: `sudo crontab -e` → `15 2 * * * bash /srv/meltice-gps-tracker/app/deploy/hostinger/backup.sh`
  (SQL Server full `.bak` in `/var/backups/meltice-gps-tracker/mssql` + env file, 14-day retention).
  Restore: `RESTORE DATABASE meltice_gps_tracker FROM DISK = '/var/opt/mssql/backup/<file>.bak' WITH REPLACE`
  in the sa console, with both services stopped.
* Port terminal events are pruned automatically after `FLEET_PORT_LOG_RETENTION_HOURS` (72 h).
* Logs rotate automatically (5 × 5 MB per process) and also go to `journalctl`.
* Change the admin password after first login (`/admin/` → Users).

## 9. Rollback

```bash
sudo systemctl disable --now fleet-receiver gunicorn-fleet
sudo rm /etc/nginx/sites-enabled/fleet.melticetech.com && sudo nginx -t && sudo systemctl reload nginx
sudo ufw delete allow 5027/tcp          # if you added it
sudo docker stop meltice-mssql          # SQL Server (data stays in /var/opt/meltice-mssql)
# remove the gps/fleet DNS records if no longer needed; restore the device's previous Configurator file
```

Data stays in `/var/lib/meltice-gps-tracker` until you delete it.

## Troubleshooting (local works, VPS fails)

| Check | Command |
|---|---|
| Listener up? | `sudo ss -ltnp \| grep 5027` |
| Service errors? | `sudo journalctl -u fleet-receiver -n 100` |
| UFW / hPanel firewall | `sudo ufw status`, hPanel firewall rules |
| DNS-only for `gps`? | `nslookup gps.melticetech.com` must return the VPS IP, not a Cloudflare IP |
| Reachable from outside? | `python tools/fmc_probe.py gps.melticetech.com 5027` from a non-VPS network |
| IMEI accepted? | *Port terminal* → `IMEI` line says accepted / rejected |
| Anything arriving at all? | *Port terminal* → `CONN` / `RX` lines (also shows scanners and wrong protocols) |
| Database reachable? | `sudo docker ps` (meltice-mssql Up), `journalctl -u gunicorn-fleet -n 50` |
