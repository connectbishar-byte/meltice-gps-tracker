from django.db import models
from django.utils import timezone


class Device(models.Model):
    imei = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    model = models.CharField(max_length=40, default="FMC130")
    enabled = models.BooleanField(default=True, help_text="Only enabled (allow-listed) IMEIs are accepted by the receiver.")
    firmware = models.CharField(max_length=40, blank=True)
    plate = models.CharField(max_length=40, blank=True)
    group = models.CharField(max_length=60, blank=True, default="Default")
    config_revision = models.CharField(max_length=60, blank=True, help_text="Configurator file / revision currently on the device")
    notes = models.TextField(blank=True)
    created_utc = models.DateTimeField(default=timezone.now)

    # denormalised status, maintained by the ingest pipeline
    last_connection_utc = models.DateTimeField(null=True, blank=True)
    last_packet_utc = models.DateTimeField(null=True, blank=True)
    last_record_ts = models.DateTimeField(null=True, blank=True)
    last_fix_ts = models.DateTimeField(null=True, blank=True)
    last_fix_lat = models.FloatField(null=True, blank=True)
    last_fix_lon = models.FloatField(null=True, blank=True)
    last_fix_speed = models.IntegerField(null=True, blank=True)
    last_fix_angle = models.IntegerField(null=True, blank=True)
    last_fix_sats = models.IntegerField(null=True, blank=True)
    last_ignition = models.BooleanField(null=True, blank=True)
    last_movement = models.BooleanField(null=True, blank=True)
    last_gsm_signal = models.IntegerField(null=True, blank=True)
    last_ext_voltage = models.FloatField(null=True, blank=True)
    packet_count = models.PositiveIntegerField(default=0)
    record_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["group", "name"]

    def __str__(self):
        return f"{self.name} ({self.masked_imei})"

    @property
    def masked_imei(self):
        return mask_imei(self.imei)


def mask_imei(imei: str) -> str:
    if not imei or len(imei) < 8:
        return imei
    return imei[:4] + "*" * (len(imei) - 8) + imei[-4:]


class Connection(models.Model):
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.SET_NULL, related_name="connections")
    claimed_imei = models.CharField(max_length=40, blank=True)
    remote = models.CharField(max_length=80)
    source = models.CharField(max_length=20, default="tcp", help_text="tcp | simulator | probe")
    opened_utc = models.DateTimeField(default=timezone.now)
    closed_utc = models.DateTimeField(null=True, blank=True)
    handshake = models.CharField(max_length=20, default="pending")  # accepted | rejected | invalid | timeout | pending
    frames = models.PositiveIntegerField(default=0)
    close_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-opened_utc"]


class RawPacket(models.Model):
    connection = models.ForeignKey(Connection, null=True, blank=True, on_delete=models.SET_NULL, related_name="packets")
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.SET_NULL, related_name="packets")
    received_utc = models.DateTimeField(default=timezone.now, db_index=True)
    hex = models.TextField()
    size = models.PositiveIntegerField()
    truncated = models.BooleanField(default=False, help_text="HEX was cut to the storage bound")
    crc_ok = models.BooleanField(null=True)
    crc_value = models.CharField(max_length=8, blank=True)
    codec = models.CharField(max_length=8, blank=True)
    record_count = models.PositiveIntegerField(default=0)
    new_records = models.PositiveIntegerField(default=0)
    duplicate_records = models.PositiveIntegerField(default=0)
    parse_status = models.CharField(max_length=12, default="ok")  # ok | error
    error = models.CharField(max_length=300, blank=True)
    ack_status = models.CharField(max_length=12, default="pending")  # sent | nack | none | pending
    ack_value = models.IntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ["-received_utc", "-id"]


class AVLRecord(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="records")
    packet = models.ForeignKey(RawPacket, on_delete=models.CASCADE, related_name="records")
    index = models.PositiveSmallIntegerField()
    timestamp_utc = models.DateTimeField(db_index=True)
    received_utc = models.DateTimeField(default=timezone.now)
    latitude = models.FloatField()
    longitude = models.FloatField()
    altitude = models.IntegerField()
    angle = models.IntegerField()
    satellites = models.IntegerField()
    speed = models.IntegerField(help_text="GNSS speed km/h from GPS element")
    priority = models.SmallIntegerField()
    event_io_id = models.IntegerField()
    valid_fix = models.BooleanField(default=False)
    late = models.BooleanField(default=False, help_text="Arrived after a newer record for the same device")
    fingerprint = models.CharField(max_length=64)

    class Meta:
        ordering = ["-timestamp_utc"]
        constraints = [models.UniqueConstraint(fields=["device", "fingerprint"], name="uniq_device_record")]
        indexes = [models.Index(fields=["device", "timestamp_utc"])]


class IOValue(models.Model):
    record = models.ForeignKey(AVLRecord, on_delete=models.CASCADE, related_name="io")
    avl_id = models.IntegerField()
    byte_length = models.SmallIntegerField()
    raw_hex = models.CharField(max_length=2048)
    value = models.FloatField(null=True, blank=True)
    text = models.CharField(max_length=200, blank=True)
    name = models.CharField(max_length=80, blank=True)
    unit = models.CharField(max_length=20, blank=True)
    known = models.BooleanField(default=True)
    mapping_version = models.CharField(max_length=40)

    class Meta:
        ordering = ["avl_id"]


class ReceiverHeartbeat(models.Model):
    """Single row per receiver instance; written every few seconds so the web UI can show listener health."""
    name = models.CharField(max_length=60, unique=True)
    host = models.CharField(max_length=60)
    port = models.IntegerField()
    started_utc = models.DateTimeField()
    last_beat_utc = models.DateTimeField()
    active_connections = models.IntegerField(default=0)
    total_connections = models.IntegerField(default=0)
    frames_ok = models.IntegerField(default=0)
    frames_bad = models.IntegerField(default=0)
    pid = models.IntegerField(default=0)


class TestRun(models.Model):
    """Result of an automated connectivity / acceptance test run (from the web UI or CLI)."""
    started_utc = models.DateTimeField(default=timezone.now)
    target = models.CharField(max_length=120)
    passed = models.BooleanField(default=False)
    summary = models.CharField(max_length=200, blank=True)
    steps = models.JSONField(default=list)

    class Meta:
        ordering = ["-started_utc"]


class PortEvent(models.Model):
    """
    Everything that happens on the tracker TCP port, in order: connections, every byte received and
    sent (HEX), handshakes, decoded frame summaries, ACKs, errors and disconnects. Feeds the live
    Port terminal page, the /api/v1/port-events/ endpoint and `manage.py port_monitor`.
    """
    KINDS = [
        ("connect", "Connection opened"), ("refused", "Connection refused"), ("rx", "Bytes received"),
        ("tx", "Bytes sent"), ("imei", "IMEI handshake"), ("frame", "Decoded frame"), ("error", "Error"),
        ("close", "Connection closed"), ("info", "Receiver info"),
    ]
    ts = models.DateTimeField(default=timezone.now, db_index=True)
    kind = models.CharField(max_length=10, choices=KINDS)
    level = models.CharField(max_length=8, default="info")  # info | ok | warn | error
    remote = models.CharField(max_length=80, blank=True, db_index=True)
    imei = models.CharField(max_length=40, blank=True, db_index=True)
    connection = models.ForeignKey(Connection, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    packet = models.ForeignKey(RawPacket, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    size = models.PositiveIntegerField(default=0)
    hex = models.TextField(blank=True)
    truncated = models.BooleanField(default=False)
    message = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.ts:%H:%M:%S} {self.kind} {self.remote} {self.message}"
