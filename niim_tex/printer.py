"""Base NIIMBOT printer class with shared BLE communication and commands."""

import asyncio
import struct

from bleak import BleakClient, BleakScanner

from .protocol import (
    InfoKey, HeartbeatType, SoundType,
    packet, parse_response,
)


class NiimbotPrinter:
    """Base class for NIIMBOT BLE printers.

    Subclasses must override:
        MODEL_PREFIXES  — BLE name prefixes for scanning (e.g. ["D110"])
        print_image()   — model-specific print sequence
    """

    MODEL_PREFIXES: list[str] = []
    DPI: int = 203                  # Native print resolution (D110=203, B1 Pro=300)
    MAX_WIDTH_PX: int = 96
    MAX_DENSITY: int = 5
    PRINTABLE_HEIGHT_MM: int = 12   # Printhead width in mm (D110=12, B1=48)

    def __init__(self):
        self.client = None
        self.char_uuid = None
        self._notify_data = None
        self._notify_event = asyncio.Event()
        self._device_name = None

    # ── BLE scanning ───────────────────────────────────────────────────

    @classmethod
    def _matches_name(cls, name):
        """Check if a BLE device name matches this printer model.

        Subclasses can override for more sophisticated matching (e.g. B1
        needs to exclude B18).  Default: prefix match against MODEL_PREFIXES.
        """
        upper = name.upper()
        return any(upper.startswith(p.upper()) for p in cls.MODEL_PREFIXES)

    async def _find_device(self, timeout=10):
        """Scan for a printer matching this model. Returns a BleakDevice."""
        found = []

        def on_detect(device, _adv):
            if device.name and self._matches_name(device.name):
                found.append(device)

        scanner = BleakScanner(detection_callback=on_detect)
        await scanner.start()
        for _ in range(timeout * 10):
            if found:
                break
            await asyncio.sleep(0.1)
        await scanner.stop()

        if not found:
            names = ", ".join(self.MODEL_PREFIXES)
            raise RuntimeError(
                f"Printer not found (scanned for: {names}). "
                "Make sure it's powered on and nearby."
            )
        return found[0]

    @staticmethod
    async def _find_char(client):
        """Find the GATT characteristic for printer communication.

        Looks for a characteristic with write-without-response + notify,
        preferring one that also has read (D110 layout).
        """
        # Strict: read + write-without-response + notify
        for service in client.services:
            for c in service.characteristics:
                props = c.properties
                if "read" in props and "write-without-response" in props and "notify" in props:
                    return c.uuid
        # Fallback: write-without-response + notify (some B-series layouts)
        for service in client.services:
            for c in service.characteristics:
                props = c.properties
                if "write-without-response" in props and "notify" in props:
                    return c.uuid
        raise RuntimeError("Could not find printer communication characteristic.")

    # ── Connection ─────────────────────────────────────────────────────

    async def connect(self):
        """Scan for and connect to the printer. Returns the device name."""
        device = await self._find_device()
        self.client = BleakClient(device.address)
        await self.client.connect()
        self.char_uuid = await self._find_char(self.client)
        self._device_name = device.name
        return device.name

    async def disconnect(self):
        """Disconnect from the printer."""
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    @property
    def is_connected(self):
        return self.client is not None and self.client.is_connected

    # ── Low-level comms ────────────────────────────────────────────────

    def _on_notify(self, _sender, data):
        # Only accept valid NIIMBOT protocol packets (0x55 0x55 header).
        # Some printers send spurious BLE notifications (e.g. 0x00000000
        # on subscribe) that must be ignored.
        if len(data) >= 7 and data[0] == 0x55 and data[1] == 0x55:
            self._notify_data = data
            self._notify_event.set()

    async def _command(self, cmd, data, timeout=10):
        """Send a command and wait for the notification response."""
        self._notify_event.clear()
        await self.client.start_notify(self.char_uuid, self._on_notify)
        await self.client.write_gatt_char(
            self.char_uuid, packet(cmd, data), response=False)
        await asyncio.wait_for(self._notify_event.wait(), timeout)
        await self.client.stop_notify(self.char_uuid)
        return parse_response(self._notify_data)

    async def _write(self, cmd, data):
        """Send a packet without waiting for a response."""
        await self.client.write_gatt_char(
            self.char_uuid, packet(cmd, data), response=False)

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
            self.char_uuid, packet(cmd, data), response=False)
        await asyncio.wait_for(self._notify_event.wait(), timeout)
        return parse_response(self._notify_data)

    async def _fire(self, cmd, data):
        """Send packet one-way, no response expected."""
        await self.client.write_gatt_char(
            self.char_uuid, packet(cmd, data), response=False)

    # ── Printer info ───────────────────────────────────────────────────

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

    # ── RFID (label roll NFC tag) ──────────────────────────────────────

    async def get_rfid(self):
        """Read the label roll's RFID tag. Returns dict or None if no tag."""
        _, data = await self._command(0x1A, b"\x01")

        if len(data) < 12 or data[0] == 0:
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

    # ── Heartbeat ──────────────────────────────────────────────────────

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

    async def wait_ready(self, delay=1.0, verbose=False):
        """Wait between consecutive prints.

        The D110_M heartbeat (10 bytes) does not contain a usable busy/ready
        signal — byte[8] is always 0x01 regardless of printer state, and
        paper_state is not populated. So we use a fixed delay instead of
        polling. The delay gives the printer time to feed paper past the
        label gap before the next print job starts.

        The print_image() method already polls print status (0xA3) until the
        page is done and sends endPrint, so the printer is finished printing
        by the time this is called. This delay only covers the physical paper
        advance.

        Args:
            delay: Seconds to wait (default 1.0). Tune with --delay flag.
            verbose: If True, log the wait.
        """
        if verbose:
            print(f"  [wait_ready] waiting {delay:.1f}s for paper advance...")
        await asyncio.sleep(delay)

    # ── Settings ───────────────────────────────────────────────────────

    async def set_label_type(self, label_type):
        """Set the label type (1=gaps, 2=black mark, 3=continuous, 5=transparent)."""
        _, data = await self._command(0x23, bytes((int(label_type),)))
        return bool(data[0])

    async def set_density(self, density):
        """Set print density (clamped to MAX_DENSITY)."""
        if density > self.MAX_DENSITY:
            density = self.MAX_DENSITY
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

    # ── Calibration & maintenance ──────────────────────────────────────

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

    # ── Print job control ──────────────────────────────────────────────

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

    # ── Print image (abstract) ─────────────────────────────────────────

    async def print_image(self, image, density=3, quantity=1, label_type=1, **kwargs):
        """Print a PIL Image. Subclasses must implement this."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement print_image(). "
            "This printer model may not be supported yet."
        )
