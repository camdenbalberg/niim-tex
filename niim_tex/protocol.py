"""NIIMBOT protocol: packet framing, enums, and shared constants."""

import enum


# ── Enums ──────────────────────────────────────────────────────────────────

class InfoKey(enum.IntEnum):
    DENSITY = 1
    SPEED = 2
    LABEL_TYPE = 3
    LANGUAGE = 6
    AUTO_SHUTDOWN_TIME = 7
    DEVICE_TYPE = 8
    SOFTWARE_VERSION = 9
    BATTERY = 10
    SERIAL = 11
    HARDWARE_VERSION = 12
    BLUETOOTH_ADDRESS = 13
    PRINT_MODE = 14
    AREA = 15


class LabelType(enum.IntEnum):
    WITH_GAPS = 1       # Standard gap-detected labels (default for D110)
    BLACK_MARK = 2      # Black mark positioning
    CONTINUOUS = 3      # Continuous roll (no gaps)
    PERFORATED = 4      # Perforated tear-off
    TRANSPARENT = 5     # Transparent/clear labels (supported on D110)


class HeartbeatType(enum.IntEnum):
    ADVANCED1 = 1       # Response: 0xDD — detailed status
    BASIC = 2           # Response: 0xDE — minimal keep-alive
    UNKNOWN = 3         # Response: 0xDF
    ADVANCED2 = 4       # Response: 0xD9 — extended status


class SoundType(enum.IntEnum):
    BLUETOOTH = 1       # Bluetooth connection sound
    POWER = 2           # Power on/off sound


class AutoShutdownTime(enum.IntEnum):
    MIN_15 = 1
    MIN_30 = 2
    MIN_45 = 3
    MIN_60 = 4          # Or "never" on some models


class PrinterError(enum.IntEnum):
    COVER_OPEN = 0x01
    NO_PAPER = 0x02
    LOW_BATTERY = 0x03
    BATTERY_EXCEPTION = 0x04
    USER_CANCEL = 0x05
    DATA_ERROR = 0x06
    OVERHEAT = 0x07
    PAPER_OUT = 0x08
    BUSY = 0x09
    NO_PRINT_HEAD = 0x0A
    TEMPERATURE_LOW = 0x0B
    PRINT_HEAD_LOOSE = 0x0C
    NO_RIBBON = 0x0D
    WRONG_RIBBON = 0x0E
    USED_RIBBON = 0x0F
    WRONG_PAPER = 0x10
    SET_PAPER_FAIL = 0x11
    SET_PRINT_MODE_FAIL = 0x12
    SET_DENSITY_FAIL = 0x13
    WRITE_RFID_FAIL = 0x14
    SET_MARGIN_FAIL = 0x15
    COMMUNICATION_EXCEPTION = 0x16
    DISCONNECT = 0x17
    CANVAS_PARAMETER_ERROR = 0x18
    ROTATION_PARAMETER_ERROR = 0x19
    ILLEGAL_PAGE = 0x32
    RECEIVE_DATA_TIMEOUT = 0x34


# ── Packet framing ────────────────────────────────────────────────────────

def packet(cmd, data):
    """Build a Niimbot protocol packet."""
    checksum = cmd ^ len(data)
    for b in data:
        checksum ^= b
    return bytes((0x55, 0x55, cmd, len(data), *data, checksum, 0xAA, 0xAA))


def connect_packet():
    """Build the special connect packet (prefixed with 0x03)."""
    pkt = packet(0xC1, b"\x01")
    return b"\x03" + pkt


def parse_response(raw):
    """Parse a Niimbot protocol packet, return (cmd, data)."""
    # Find the packet start — some responses may have leading bytes
    start = raw.find(b"\x55\x55")
    if start < 0:
        raise ValueError(f"Invalid packet: no header found in {raw.hex()}")
    end = raw.find(b"\xaa\xaa", start)
    if end < 0:
        raise ValueError(f"Invalid packet: no footer found in {raw.hex()}")
    pkt = raw[start:end + 2]
    cmd = pkt[2]
    length = pkt[3]
    data = pkt[4:4 + length]
    return cmd, data
