"""Minimal NIIMBOT D110 BLE driver for bleak 3.x."""

import asyncio
import math
import struct

from bleak import BleakClient, BleakScanner
from PIL import Image, ImageOps

MAX_WIDTH = 240


def _packet(cmd, data):
    """Build a Niimbot protocol packet."""
    checksum = cmd ^ len(data)
    for b in data:
        checksum ^= b
    return bytes((0x55, 0x55, cmd, len(data), *data, checksum, 0xAA, 0xAA))


def _parse_response(raw):
    """Parse a Niimbot protocol packet, return (type, data)."""
    assert raw[:2] == b"\x55\x55" and raw[-2:] == b"\xaa\xaa"
    cmd = raw[2]
    length = raw[3]
    data = raw[4:4 + length]
    return cmd, data


async def find_d110(timeout=10):
    """Scan for a D110 printer via BLE. Returns (name, address)."""
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
        raise RuntimeError("D110 not found. Make sure it's powered on and paired.")
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


class D110:
    def __init__(self):
        self.client = None
        self.char_uuid = None
        self._notify_data = None
        self._notify_event = asyncio.Event()

    async def connect(self):
        device = await find_d110()
        self.client = BleakClient(device.address)
        await self.client.connect()
        self.char_uuid = await _find_char(self.client)
        return device.name

    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    def _on_notify(self, _sender, data):
        self._notify_data = data
        self._notify_event.set()

    async def _command(self, cmd, data, timeout=10):
        """Send a command and wait for the notification response."""
        self._notify_event.clear()
        await self.client.start_notify(self.char_uuid, self._on_notify)
        await self.client.write_gatt_char(self.char_uuid, _packet(cmd, data))
        await asyncio.wait_for(self._notify_event.wait(), timeout)
        await self.client.stop_notify(self.char_uuid)
        return _parse_response(self._notify_data)

    async def _write(self, cmd, data):
        """Send a packet without waiting for a response."""
        await self.client.write_gatt_char(self.char_uuid, _packet(cmd, data))

    # --- Info commands ---

    async def get_info(self):
        """Return dict with serial, software version, hardware version, battery."""
        _, serial_data = await self._command(0x40, bytes((11,)))
        _, sw_data = await self._command(0x40, bytes((9,)))
        _, hw_data = await self._command(0x40, bytes((12,)))
        _, bat_data = await self._command(0x40, bytes((10,)))
        return {
            "serial": serial_data.hex(),
            "software": int.from_bytes(sw_data, "big") / 100,
            "hardware": int.from_bytes(hw_data, "big") / 100,
            "battery": int.from_bytes(bat_data, "big"),
        }

    # --- Print commands ---

    async def print_image(self, image: Image.Image, density=3, quantity=1):
        """Print a PIL Image to the D110."""
        if density > 3:
            density = 3  # D110 caps at 3

        assert image.width <= MAX_WIDTH, f"Image too wide: {image.width}px > {MAX_WIDTH}px"

        # Convert to monochrome bitmap
        img = ImageOps.invert(image.convert("L")).convert("1")

        # Set up print job
        await self._command(0x21, bytes((density,)))       # set density
        await self._command(0x23, bytes((1,)))              # set label type
        await self._command(0x01, b"\x01")                  # start print
        await self._command(0x03, b"\x01")                  # start page
        await self._command(0x13, struct.pack(">HH", img.height, img.width))  # set dimension
        await self._command(0x15, struct.pack(">H", quantity))  # set quantity

        # Send image line by line
        for y in range(img.height):
            bits = "".join("0" if img.getpixel((x, y)) == 0 else "1" for x in range(img.width))
            row_bytes = int(bits, 2).to_bytes(math.ceil(img.width / 8), "big")
            header = struct.pack(">H3BB", y, 0, 0, 0, 1)
            await self._write(0x85, header + row_bytes)
            await asyncio.sleep(0.01)

        # Finish up
        while True:
            _, data = await self._command(0xE3, b"\x01")  # end page
            if data[0]:
                break
            await asyncio.sleep(0.05)

        # Wait for all copies to finish
        while True:
            _, data = await self._command(0xA3, b"\x01")  # print status
            page = struct.unpack(">H", data[:2])[0]
            if page >= quantity:
                break
            await asyncio.sleep(0.1)

        await self._command(0xF3, b"\x01")  # end print
