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

        # endPage
        time.sleep(0.5)
        for _ in range(300):
            try:
                _, data = self._usb_cmd(0xE3, b"\x01")
                if data[0]:
                    break
            except Exception:
                pass
            time.sleep(0.2)

        # Poll status
        for _ in range(600):
            try:
                _, data = self._usb_cmd(0xA3, b"\x01")
                if len(data) >= 2:
                    page = struct.unpack(">H", data[:2])[0]
                    if page >= quantity:
                        break
            except Exception:
                pass
            time.sleep(0.2)

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
