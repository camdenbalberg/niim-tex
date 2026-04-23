"""NIIMBOT B1 / B1 Pro print driver (standard protocol)."""

import asyncio
import math
import struct
import time

from PIL import ImageOps

from ..printer import NiimbotPrinter
from ..protocol import LabelType, packet as build_packet, parse_response


class B1Printer(NiimbotPrinter):
    """Driver for the NIIMBOT B1 / B1 Pro (standard protocol).

    Supports both BLE and USB serial transports. USB is preferred when
    available — it's ~10x faster than BLE on Windows.
    """

    MODEL_PREFIXES = ["B1"]
    DPI = 300
    MAX_WIDTH_PX = 591
    MAX_DENSITY = 5
    PRINTABLE_HEIGHT_MM = 50
    DEFAULT_GAMMA = 0.55

    def __init__(self):
        super().__init__()
        self._usb = None  # UsbTransport when connected via USB

    @classmethod
    def _matches_name(cls, name):
        upper = name.upper()
        if not upper.startswith("B1"):
            return False
        if len(upper) > 2 and upper[2].isalnum() and upper[2] != "_":
            return False
        return True

    # ── USB connection ───────────────────────────────────────────────

    def connect_usb(self, port=None):
        """Connect via USB serial. Returns port name."""
        from ..transport import UsbTransport
        self._usb = UsbTransport(port)
        name = self._usb.connect()
        self._device_name = f"USB:{name}"
        return self._device_name

    async def connect(self):
        """Connect via BLE (async). For USB, use connect_usb() instead."""
        return await super().connect()

    async def disconnect(self):
        if self._usb:
            self._usb.disconnect()
            self._usb = None
        else:
            await super().disconnect()

    @property
    def is_connected(self):
        if self._usb:
            return self._usb.is_connected
        return super().is_connected

    # ── Unified command interface ────────────────────────────────────

    def _usb_cmd(self, cmd, data):
        """Send command over USB and return (cmd, data) response."""
        return self._usb.command(cmd, data)

    def _usb_write(self, raw_bytes):
        """Send raw bytes over USB (no response wait)."""
        self._usb.write_raw(raw_bytes)

    # Override base class methods to route through USB when connected
    async def get_info(self):
        if self._usb:
            return self._get_info_usb()
        return await super().get_info()

    def _get_info_usb(self):
        from ..protocol import InfoKey
        result = {}
        _, data = self._usb_cmd(0x40, bytes((InfoKey.SERIAL,)))
        result["serial"] = data.hex()
        _, data = self._usb_cmd(0x40, bytes((InfoKey.SOFTWARE_VERSION,)))
        result["software"] = int.from_bytes(data, "big") / 100
        _, data = self._usb_cmd(0x40, bytes((InfoKey.HARDWARE_VERSION,)))
        result["hardware"] = int.from_bytes(data, "big") / 100
        _, data = self._usb_cmd(0x40, bytes((InfoKey.BATTERY,)))
        result["battery"] = min(int.from_bytes(data, "big") * 25, 100)
        _, data = self._usb_cmd(0x40, bytes((InfoKey.DENSITY,)))
        result["density"] = int.from_bytes(data, "big")
        _, data = self._usb_cmd(0x40, bytes((InfoKey.SPEED,)))
        result["speed"] = int.from_bytes(data, "big")
        _, data = self._usb_cmd(0x40, bytes((InfoKey.LABEL_TYPE,)))
        result["label_type"] = int.from_bytes(data, "big")
        _, data = self._usb_cmd(0x40, bytes((InfoKey.DEVICE_TYPE,)))
        result["device_type"] = int.from_bytes(data, "big")
        for key, name, fmt in [
            (InfoKey.BLUETOOTH_ADDRESS, "bluetooth_address", "hex"),
            (InfoKey.AUTO_SHUTDOWN_TIME, "auto_shutdown_time", "int"),
        ]:
            try:
                _, data = self._usb_cmd(0x40, bytes((key,)))
                result[name] = data.hex() if fmt == "hex" else int.from_bytes(data, "big")
            except Exception:
                pass
        return result

    async def get_rfid(self):
        if self._usb:
            return self._get_rfid_usb()
        return await super().get_rfid()

    def _get_rfid_usb(self):
        import struct as _s
        _, data = self._usb_cmd(0x1A, b"\x01")
        if len(data) < 12 or data[0] == 0:
            return None
        uuid = data[0:8].hex()
        idx = 8
        barcode_len = data[idx]; idx += 1
        barcode = data[idx:idx+barcode_len].decode(errors="replace"); idx += barcode_len
        serial_len = data[idx]; idx += 1
        serial = data[idx:idx+serial_len].decode(errors="replace"); idx += serial_len
        total_len, used_len, type_ = _s.unpack(">HHB", data[idx:idx+5])
        return {
            "uuid": uuid, "barcode": barcode, "serial": serial,
            "total_labels": total_len, "used_labels": used_len,
            "remaining_labels": total_len - used_len, "type": type_,
        }

    async def heartbeat(self, hb_type=None):
        if self._usb:
            return self._heartbeat_usb()
        from ..protocol import HeartbeatType
        return await super().heartbeat(hb_type or HeartbeatType.ADVANCED1)

    async def get_sound(self, sound_type):
        if self._usb:
            payload = bytes((0x02, int(sound_type), 0x01))
            _, data = self._usb_cmd(0x58, payload)
            return bool(data[-1]) if data else None
        return await super().get_sound(sound_type)

    async def set_sound(self, sound_type, enabled):
        if self._usb:
            payload = bytes((0x01, int(sound_type), 0x01 if enabled else 0x00))
            _, data = self._usb_cmd(0x58, payload)
            return bool(data[0]) if data else True
        return await super().set_sound(sound_type, enabled)

    async def set_auto_shutdown_time(self, time_setting):
        if self._usb:
            _, data = self._usb_cmd(0x27, bytes((int(time_setting),)))
            return bool(data[0])
        return await super().set_auto_shutdown_time(time_setting)

    async def set_density(self, density):
        if self._usb:
            _, data = self._usb_cmd(0x21, bytes((density,)))
            return bool(data[0])
        return await super().set_density(density)

    async def calibrate_label(self, value=1):
        if self._usb:
            _, data = self._usb_cmd(0x8E, bytes((value,)))
            return bool(data[0]) if data else True
        return await super().calibrate_label(value)

    async def print_test_page(self):
        if self._usb:
            _, data = self._usb_cmd(0x5A, b"\x01")
            return bool(data[0]) if data else True
        return await super().print_test_page()

    async def cancel_print(self):
        if self._usb:
            _, data = self._usb_cmd(0xDA, b"\x01")
            return bool(data[0]) if data else True
        return await super().cancel_print()

    def _heartbeat_usb(self):
        _, data = self._usb_cmd(0xDC, b"\x01")
        result = {"raw": data.hex(), "closing_state": None,
                  "power_level": None, "paper_state": None, "rfid_read_state": None}
        n = len(data)
        if n >= 20:
            result["paper_state"] = data[18]; result["rfid_read_state"] = data[19]
        elif n >= 13:
            result["closing_state"] = data[9]; result["power_level"] = data[10]
            result["paper_state"] = data[11]; result["rfid_read_state"] = data[12]
        elif n >= 10:
            result["closing_state"] = data[8]; result["power_level"] = data[9]
        return result

    # ── Print image ──────────────────────────────────────────────────

    async def print_image(self, image, density=3, quantity=1,
                          label_type=LabelType.WITH_GAPS,
                          vertical_offset=0, horizontal_offset=0):
        if density > self.MAX_DENSITY:
            density = self.MAX_DENSITY

        img = ImageOps.invert(image.convert("L")).convert("1")

        if horizontal_offset > 0:
            img = ImageOps.expand(img, border=(horizontal_offset, 0, 0, 0), fill=0)
        elif horizontal_offset < 0:
            img = img.crop((-horizontal_offset, 0, img.width, img.height))
        if vertical_offset > 0:
            img = ImageOps.expand(img, border=(0, vertical_offset, 0, 0), fill=0)

        if img.width > self.MAX_WIDTH_PX:
            raise ValueError(f"Image too wide: {img.width}px > {self.MAX_WIDTH_PX}px")

        # Pre-encode all row packets
        row_width_bytes = math.ceil(img.width / 8)
        packed = img.tobytes("raw", "1")
        packets = []
        for y in range(img.height):
            row_bytes = packed[y * row_width_bytes : (y + 1) * row_width_bytes]
            header = struct.pack(">H3BB", y, 0, 0, 0, 1)
            packets.append(build_packet(0x85, header + row_bytes))

        if self._usb:
            self._print_usb(img, packets, density, quantity, label_type)
        else:
            await self._print_ble(img, packets, density, quantity, label_type)

    # ── USB print path (synchronous, fast) ───────────────────────────

    def _print_usb(self, img, packets, density, quantity, label_type):
        """Print over USB serial — no BLE overhead."""
        t0 = time.time()

        self._usb_cmd(0x21, bytes((density,)))
        self._usb_cmd(0x23, bytes((int(label_type),)))
        self._usb_cmd(0x01, b"\x01")
        self._usb_cmd(0x03, b"\x01")
        self._usb_cmd(0x13, struct.pack(">HH", img.height, img.width))
        self._usb_cmd(0x15, struct.pack(">H", quantity))

        # Send all rows in one burst — USB CDC handles flow control
        for pkt in packets:
            self._usb.ser.write(pkt)
        self._usb.ser.flush()

        print(f"  [USB] All {len(packets)} rows sent in "
              f"{time.time()-t0:.1f}s")

        # Printer prints as data arrives over USB — by the time the last
        # row is sent, the printer is nearly done. Wait for the last
        # rows to render before closing out.
        time.sleep(4)

        try:
            self._usb_cmd(0xE3, b"\x01")
        except Exception:
            pass
        self._usb_cmd(0xF3, b"\x01")
        print(f"  [USB] Total: {time.time()-t0:.1f}s")

    # ── BLE print path (async, slower) ───────────────────────────────

    async def _print_ble(self, img, packets, density, quantity, label_type):
        """Print over BLE — write-with-response."""
        self._notify_event = asyncio.Event()
        await self._start_notify()

        try:
            await self._b1_cmd(0x21, bytes((density,)))
            await self._b1_cmd(0x23, bytes((int(label_type),)))
            await self._b1_cmd(0x01, b"\x01")
            await self._b1_cmd(0x03, b"\x01")
            await self._b1_cmd(0x13, struct.pack(">HH", img.height, img.width))
            await self._b1_cmd(0x15, struct.pack(">H", quantity))

            for pkt in packets:
                await self.client.write_gatt_char(self.char_uuid, pkt)

            await self._stop_notify()
            await asyncio.sleep(1.0)

            for _ in range(300):
                try:
                    _, data = await self._command(0xE3, b"\x01")
                    if data[0]:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.2)

            for _ in range(600):
                try:
                    _, data = await self._command(0xA3, b"\x01")
                    if len(data) >= 2:
                        page = struct.unpack(">H", data[:2])[0]
                        if page >= quantity:
                            break
                except Exception:
                    pass
                await asyncio.sleep(0.2)

            await self._command(0xF3, b"\x01")

        finally:
            try:
                await self._stop_notify()
            except Exception:
                pass

    async def _b1_cmd(self, cmd, data, timeout=10):
        self._notify_event.clear()
        await self.client.write_gatt_char(
            self.char_uuid, build_packet(cmd, data))
        await asyncio.wait_for(self._notify_event.wait(), timeout)
        return parse_response(self._notify_data)
