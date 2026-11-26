from django.contrib import admin

from .models import AVLRecord, Connection, Device, IOValue, RawPacket, ReceiverHeartbeat, TestRun, PortEvent


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "masked_imei", "model", "enabled", "firmware", "last_packet_utc", "packet_count", "error_count")
    list_filter = ("enabled", "model", "group")
    search_fields = ("name", "imei", "plate")


@admin.register(Connection)
class ConnectionAdmin(admin.ModelAdmin):
    list_display = ("opened_utc", "device", "claimed_imei", "remote", "source", "handshake", "frames", "close_reason")
    list_filter = ("handshake", "source")


@admin.register(RawPacket)
class RawPacketAdmin(admin.ModelAdmin):
    list_display = ("received_utc", "device", "codec", "size", "crc_ok", "record_count", "parse_status", "ack_status")
    list_filter = ("parse_status", "ack_status", "codec")


class IOInline(admin.TabularInline):
    model = IOValue
    extra = 0


@admin.register(AVLRecord)
class AVLRecordAdmin(admin.ModelAdmin):
    list_display = ("timestamp_utc", "device", "latitude", "longitude", "speed", "satellites", "valid_fix", "late")
    list_filter = ("valid_fix", "late", "device")
    inlines = [IOInline]


admin.site.register(ReceiverHeartbeat)
admin.site.register(TestRun)


@admin.register(PortEvent)
class PortEventAdmin(admin.ModelAdmin):
    list_display = ("id", "ts", "kind", "level", "remote", "imei", "size", "message")
    list_filter = ("kind", "level")
    search_fields = ("imei", "remote", "message", "hex")
    readonly_fields = [f.name for f in PortEvent._meta.fields]
