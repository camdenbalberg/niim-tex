"""NIIMBOT D110 BLE driver — full protocol implementation for bleak 3.x."""

import asyncio
import enum
import math
import struct

from bleak import BleakClient, BleakScanner
from PIL import Image, ImageOps

MAX_WIDTH = 240


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

def _packet(cmd, data):
    """Build a Niimbot protocol packet."""
    checksum = cmd ^ len(data)
    for b in data:
        checksum ^= b
    return bytes((0x55, 0x55, cmd, len(data), *data, checksum, 0xAA, 0xAA))


def _connect_packet():
    """Build the special connect packet (prefixed with 0x03)."""
    pkt = _packet(0xC1, b"\x01")
    return b"\x03" + pkt


def _parse_response(raw):
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


# ── BLE scanning ──────────────────────────────────────────────────────────

async def find_d110(timeout=10):
    """Scan for a D110 printer via BLE. Returns a BleakDevice."""
    found = []

    def on_detect(device, _adv):
        if device.name and device.name.upper().startswith("D110"):
            found.append(device)

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    for _ in range(timeout * 10):
        if found:
            break
        await asyncio.sleep(0.1)
    await scanner.stop()

    if not found:
        raise RuntimeError("D110 not found. Make sure it's powered on and nearby.")
    return found[0]


async def _find_char(client):
    """Find the D110's GATT characteristic (read + write-no-response + notify)."""
    for service in client.services:
        chars = list(service.characteristics)
        if len(chars) == 1:
            c = chars[0]
            props = c.properties
            if "read" in props and "write-without-response" in props and "notify" in props:
                return c.uuid
    raise RuntimeError("Could not find D110 communication characteristic.")


# ── D110 driver ───────────────────────────────────────────────────────────

class D110:
    def __init__(self):
        self.client = None
        self.char_uuid = None
        self._notify_data = None
        self._notify_event = asyncio.Event()
        self._device_name = None

    # ── Connection ────────────────────────────────────────────────────

    async def connect(self):
        """Scan for and connect to the D110. Returns the device name."""
        device = await find_d110()
        self.client = BleakClient(device.address)
        await self.client.connect()
        self.char_uuid = await _find_char(self.client)
        self._device_name = device.name
        return device.name

    async def disconnect(self):
        """Disconnect from the printer."""
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    @property
    def is_connected(self):
        return self.client is not None and self.client.is_connected

    # ── Low-level comms ───────────────────────────────────────────────

    def _on_notify(self, _sender, data):
        self._notify_data = data
        self._notify_event.set()

    async def _command(self, cmd, data, timeout=10):
        """Send a command and wait for the notification response."""
        self._notify_event.clear()
        await self.client.start_notify(self.char_uuid, self._on_notify)
        await self.client.write_gatt_char(
            self.char_uuid, _packet(cmd, data), response=False)
        await asyncio.wait_for(self._notify_event.wait(), timeout)
        await self.client.stop_notify(self.char_uuid)
        return _parse_response(self._notify_data)

    async def _write(self, cmd, data):
        """Send a packet without waiting for a response."""
        await self.client.write_gatt_char(
            self.char_uuid, _packet(cmd, data), response=False)

    async def _write_raw(self, raw_bytes):
        """Send raw bytes (for the connect packet with 0x03 prefix)."""
        await self.client.write_gatt_char(
            self.char_uuid, raw_bytes, response=False)

    async def _start_notify(self):
        """Start persistent BLE notifications (for print sessions)."""
        self._notify_event.clear()
        await self.client.start_notify(self.char_uuid, self._on_notify)

    async def _stop_notify(self):
        """Stop BLE notifications."""
        await self.client.stop_notify(self.char_uuid)

    async def _cmd(self, cmd, data, timeout=10):
        """Send command and wait for response (notifications must already be active)."""
        self._notify_event.clear()
        await self.client.write_gatt_char(
            self.char_uuid, _packet(cmd, data), response=False)
        await asyncio.wait_for(self._notify_event.wait(), timeout)
        return _parse_response(self._notify_data)

    async def _fire(self, cmd, data):
        """Send packet one-way, no response expected."""
        await self.client.write_gatt_char(
            self.char_uuid, _packet(cmd, data), response=False)

    # ── Printer info ──────────────────────────────────────────────────

    async def get_info_raw(self, key):
        """Query a single info key, return raw bytes."""
        _, data = await self._command(0x40, bytes((int(key),)))
        return data

    async def get_info(self):
        """Return a dict with all queryable printer info."""
        result = {}

        _, serial_data = await self._command(0x40, bytes((InfoKey.SERIAL,)))
        result["serial"] = serial_data.hex()

        _, sw_data = await self._command(0x40, bytes((InfoKey.SOFTWARE_VERSION,)))
        result["software"] = int.from_bytes(sw_data, "big") / 100

        _, hw_data = await self._command(0x40, bytes((InfoKey.HARDWARE_VERSION,)))
        result["hardware"] = int.from_bytes(hw_data, "big") / 100

        _, bat_data = await self._command(0x40, bytes((InfoKey.BATTERY,)))
        bat_level = int.from_bytes(bat_data, "big")
        result["battery"] = min(bat_level * 25, 100)

        _, density_data = await self._command(0x40, bytes((InfoKey.DENSITY,)))
        result["density"] = int.from_bytes(density_data, "big")

        _, speed_data = await self._command(0x40, bytes((InfoKey.SPEED,)))
        result["speed"] = int.from_bytes(speed_data, "big")

        _, lt_data = await self._command(0x40, bytes((InfoKey.LABEL_TYPE,)))
        result["label_type"] = int.from_bytes(lt_data, "big")

        _, dev_data = await self._command(0x40, bytes((InfoKey.DEVICE_TYPE,)))
        result["device_type"] = int.from_bytes(dev_data, "big")

        # These may not be supported on all firmware — catch failures
        for key, name, fmt in [
            (InfoKey.BLUETOOTH_ADDRESS, "bluetooth_address", "hex"),
            (InfoKey.LANGUAGE, "language", "int"),
            (InfoKey.AUTO_SHUTDOWN_TIME, "auto_shutdown_time", "int"),
            (InfoKey.PRINT_MODE, "print_mode", "int"),
            (InfoKey.AREA, "area", "int"),
        ]:
            try:
                _, data = await self._command(0x40, bytes((key,)))
                if fmt == "hex":
                    result[name] = data.hex()
                else:
                    result[name] = int.from_bytes(data, "big")
            except Exception:
                pass  # Not supported on this firmware

        return result

    # ── RFID (label roll NFC tag) ─────────────────────────────────────

    async def get_rfid(self):
        """Read the label roll's RFID tag. Returns dict or None if no tag."""
        _, data = await self._command(0x1A, b"\x01")

        if len(data) < 1 or data[0] == 0:
            return None

        uuid = data[0:8].hex()
        idx = 8

        barcode_len = data[idx]
        idx += 1
        barcode = data[idx:idx + barcode_len].decode(errors="replace")
        idx += barcode_len

        serial_len = data[idx]
        idx += 1
        serial = data[idx:idx + serial_len].decode(errors="replace")
        idx += serial_len

        total_len, used_len, type_ = struct.unpack(">HHB", data[idx:idx + 5])
        remaining = total_len - used_len

        return {
            "uuid": uuid,
            "barcode": barcode,
            "serial": serial,
            "total_labels": total_len,
            "used_labels": used_len,
            "remaining_labels": remaining,
            "type": type_,
        }

    # ── Heartbeat ─────────────────────────────────────────────────────

    async def heartbeat(self, hb_type=HeartbeatType.ADVANCED1):
        """Send a heartbeat and parse the response.

        Returns a dict with available fields depending on heartbeat type:
        - closing_state: lid closed (0=closed, 1=open)
        - power_level: battery level
        - paper_state: paper inserted (0=inserted)
        - rfid_read_state: RFID read status
        """
        _, data = await self._command(0xDC, bytes((int(hb_type),)))

        result = {
            "raw": data.hex(),
            "closing_state": None,
            "power_level": None,
            "paper_state": None,
            "rfid_read_state": None,
        }

        n = len(data)
        if n >= 20:
            result["paper_state"] = data[18]
            result["rfid_read_state"] = data[19]
        elif n >= 13:
            result["closing_state"] = data[9]
            result["power_level"] = data[10]
            result["paper_state"] = data[11]
            result["rfid_read_state"] = data[12]
        elif n >= 10:
            result["closing_state"] = data[8]
            result["power_level"] = data[9]
            result["rfid_read_state"] = data[8]
        elif n >= 9:
            result["closing_state"] = data[8]

        return result

    # ── Settings ──────────────────────────────────────────────────────

    async def set_label_type(self, label_type):
        """Set the label type (1=gaps, 2=black mark, 3=continuous, 5=transparent)."""
        _, data = await self._command(0x23, bytes((int(label_type),)))
        return bool(data[0])

    async def set_density(self, density):
        """Set print density (1-5 for D110)."""
        if density > 5:
            density = 5
        _, data = await self._command(0x21, bytes((density,)))
        return bool(data[0])

    async def set_auto_shutdown_time(self, time_setting):
        """Set auto-shutdown timer (1=15min, 2=30min, 3=45min, 4=60min)."""
        _, data = await self._command(0x27, bytes((int(time_setting),)))
        return bool(data[0])

    async def set_sound(self, sound_type, enabled):
        """Enable or disable a sound (SoundType.BLUETOOTH or SoundType.POWER)."""
        payload = bytes((0x01, int(sound_type), 0x01 if enabled else 0x00))
        _, data = await self._command(0x58, payload)
        return bool(data[0]) if data else True

    async def get_sound(self, sound_type):
        """Query whether a sound is enabled."""
        payload = bytes((0x02, int(sound_type), 0x01))
        _, data = await self._command(0x58, payload)
        # Response format varies, but last byte is typically the state
        return bool(data[-1]) if data else None

    # ── Calibration & maintenance ─────────────────────────────────────

    async def calibrate_label(self, value=1):
        """Feed ~15cm of paper to recalibrate label positioning."""
        _, data = await self._command(0x8E, bytes((value,)), timeout=30)
        return bool(data[0]) if data else True

    async def calibrate_height(self):
        """Run height calibration."""
        _, data = await self._command(0x59, b"\x01", timeout=30)
        return bool(data[0]) if data else True

    async def print_test_page(self):
        """Print the built-in test page."""
        _, data = await self._command(0x5A, b"\x01", timeout=30)
        return bool(data[0]) if data else True

    async def printer_reset(self):
        """Reset the printer to factory settings."""
        _, data = await self._command(0x28, b"\x01")
        return bool(data[0]) if data else True

    async def cancel_print(self):
        """Cancel an ongoing print job."""
        _, data = await self._command(0xDA, b"\x01")
        return bool(data[0]) if data else True

    # ── Print job control ─────────────────────────────────────────────

    async def start_print(self, total_pages=1):
        """Start print job. Uses 9-byte variant for D110_M."""
        payload = struct.pack(">H7B", total_pages, 0, 0, 0, 0, 0, 0, 0)
        _, data = await self._command(0x01, payload)
        return bool(data[0])

    async def end_print(self):
        _, data = await self._command(0xF3, b"\x01")
        return bool(data[0])

    async def print_clear(self):
        """Clear the print buffer (sent before each page)."""
        _, data = await self._command(0x20, b"\x01")
        return bool(data[0])

    async def start_page(self):
        _, data = await self._command(0x03, b"\x01")
        return bool(data[0])

    async def end_page(self):
        _, data = await self._command(0xE3, b"\x01")
        return bool(data[0])

    async def set_dimension(self, height, width):
        _, data = await self._command(0x13, struct.pack(">HH", height, width))
        return bool(data[0])

    async def set_quantity(self, quantity):
        _, data = await self._command(0x15, struct.pack(">H", quantity))
        return bool(data[0])

    async def get_print_status(self):
        """Poll print status. Returns dict with page count and progress."""
        _, data = await self._command(0xA3, b"\x01")
        page = struct.unpack(">H", data[:2])[0]
        progress1 = data[2] if len(data) > 2 else 0
        progress2 = data[3] if len(data) > 3 else 0
        return {"page": page, "progress1": progress1, "progress2": progress2}

    # ── Image encoding ────────────────────────────────────────────────

    @staticmethod
    def _pixel_counts(row_bytes, width_px):
        """Count black pixels in 3 chunks (split mode for D110_M row header)."""
        chunk_size = math.ceil(width_px / 8 / 3)
        counts = [0, 0, 0]
        for i, b in enumerate(row_bytes):
            chunk = min(i // chunk_size, 2)
            for bit in range(8):
                if b & (0x80 >> bit):
                    counts[chunk] += 1
        return counts

    # ── Print image (full pipeline) ───────────────────────────────────

    async def print_image(self, image, density=3, quantity=1,
                          label_type=LabelType.WITH_GAPS,
                          vertical_offset=0, horizontal_offset=0):
        """Print a PIL Image to the D110_M using the V4 print sequence.

        Args:
            image: PIL Image to print.
            density: Print darkness 1-5 (default 3).
            quantity: Number of copies (default 1).
            label_type: LabelType enum (default WITH_GAPS).
            vertical_offset: Pixel rows of blank space to add above image.
            horizontal_offset: Pixel columns to shift image (positive=right, negative=left).
        """
        if density > 5:
            density = 5

        # Convert to monochrome: invert so 1=black (heat), 0=white (no heat)
        img = ImageOps.invert(image.convert("L")).convert("1")

        # Apply horizontal offset
        if horizontal_offset > 0:
            img = ImageOps.expand(img, border=(horizontal_offset, 0, 0, 0), fill=0)
        elif horizontal_offset < 0:
            img = img.crop((-horizontal_offset, 0, img.width, img.height))

        # Apply vertical offset
        if vertical_offset > 0:
            img = ImageOps.expand(img, border=(0, vertical_offset, 0, 0), fill=0)

        if img.width > MAX_WIDTH:
            raise ValueError(f"Image too wide: {img.width}px > {MAX_WIDTH}px")

        # Start persistent notifications for the print session
        await self._start_notify()

        try:
            # 1. Init
            await self._cmd(0x21, bytes((density,)))
            await self._cmd(0x23, bytes((int(label_type),)))
            await self._cmd(0x01, struct.pack(">H7B", quantity, 0, 0, 0, 0, 0, 0, 0))

            # 2. Throwaway one-way status (D110_M BLE workaround)
            await self._fire(0xA3, b"\x01")
            await asyncio.sleep(0.2)
            self._notify_event.clear()

            # 3. setPageSize13b: rows, cols, copies, cutHeight, cutType, 0, sendAll, partHeight
            page_size = struct.pack(">HHHH3BH",
                                    img.height, img.width, quantity, 0, 0, 0, 0, 0)
            await self._cmd(0x13, page_size)

            # 4. Send image rows with pixel counts
            for y in range(img.height):
                bits = "".join(
                    "0" if img.getpixel((x, y)) == 0 else "1"
                    for x in range(img.width))
                row_bytes = int(bits, 2).to_bytes(
                    math.ceil(img.width / 8), "big")
                counts = self._pixel_counts(row_bytes, img.width)
                header = struct.pack(">H3BB", y,
                                     counts[0], counts[1], counts[2], 1)
                await self._fire(0x85, header + row_bytes)
                await asyncio.sleep(0.01)

            await asyncio.sleep(0.5)

            # 5. pageEnd
            await self._cmd(0xE3, b"\x01")

            # 6. Poll status until all copies done
            for _ in range(100):
                self._notify_event.clear()
                await self._fire(0xA3, b"\x01")
                await asyncio.wait_for(self._notify_event.wait(), 10)
                _, data = _parse_response(self._notify_data)
                page = struct.unpack(">H", data[:2])[0]
                if page >= quantity:
                    break
                await asyncio.sleep(0.2)

            # 7. endPrint
            await self._cmd(0xF3, b"\x01")

            # 8. One-way heartbeat after endPrint (BLE workaround)
            await self._fire(0xDC, b"\x01")

        finally:
            await self._stop_notify()
