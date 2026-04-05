"""NIIMBOT B1 / B1 Pro print driver (standard protocol)."""

import asyncio
import math
import struct

from PIL import ImageOps

from ..printer import NiimbotPrinter
from ..protocol import LabelType, packet as build_packet, parse_response


class B1Printer(NiimbotPrinter):
    """Driver for the NIIMBOT B1 / B1 Pro (standard protocol).

    Protocol based on NiimPrintX reference implementation.  Key differences
    from the D110_M V4 variant:
    - startPrint uses a 1-byte payload
    - startPage (0x03) is required
    - setDimension uses 4 bytes (height, width) instead of 13
    - setQuantity is a separate command (not embedded in setPageSize)
    - Image rows use zero counts in the 6-byte header (same struct as D110)
    - All BLE writes use write-with-response (not write-without-response)
    - No one-way heartbeat workaround needed after endPrint
    """

    MODEL_PREFIXES = ["B1"]
    DPI = 300             # B1 Pro native resolution
    MAX_WIDTH_PX = 591    # 50mm printhead at 300 DPI
    MAX_DENSITY = 5
    PRINTABLE_HEIGHT_MM = 50
    DEFAULT_GAMMA = 0.55  # Compensate for finer 300 DPI dots filling in darker

    @classmethod
    def _matches_name(cls, name):
        """Match B1 but not B18, B1S, etc."""
        upper = name.upper()
        if not upper.startswith("B1"):
            return False
        # Next char must not be alphanumeric (to exclude B18, B1S, etc.)
        if len(upper) > 2 and upper[2].isalnum() and upper[2] != "_":
            return False
        return True

    async def print_image(self, image, density=3, quantity=1,
                          label_type=LabelType.WITH_GAPS,
                          vertical_offset=0, horizontal_offset=0):
        """Print a PIL Image using the B1 standard print sequence.

        Args:
            image: PIL Image to print.
            density: Print darkness 1-5 (default 3).
            quantity: Number of copies (default 1).
            label_type: LabelType enum (default WITH_GAPS).
            vertical_offset: Pixel rows of blank space to add above image.
            horizontal_offset: Pixel columns to shift image.
        """
        if density > self.MAX_DENSITY:
            density = self.MAX_DENSITY

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

        if img.width > self.MAX_WIDTH_PX:
            raise ValueError(f"Image too wide: {img.width}px > {self.MAX_WIDTH_PX}px")

        # Fresh event to avoid cross-loop issues from multiple asyncio.run() calls
        self._notify_event = asyncio.Event()

        # Use persistent notifications for the entire print session
        await self._start_notify()

        try:
            # 1. Setup (write-with-response, wait for notification)
            await self._b1_cmd(0x21, bytes((density,)))
            await self._b1_cmd(0x23, bytes((int(label_type),)))
            await self._b1_cmd(0x01, b"\x01")                              # startPrint
            await self._b1_cmd(0x03, b"\x01")                              # startPage
            await self._b1_cmd(0x13, struct.pack(">HH", img.height, img.width))
            await self._b1_cmd(0x15, struct.pack(">H", quantity))          # setQuantity

            # 2. Send image rows — write-with-response, 6-byte header
            for y in range(img.height):
                bits = "".join(
                    "0" if img.getpixel((x, y)) == 0 else "1"
                    for x in range(img.width))
                row_bytes = int(bits, 2).to_bytes(
                    math.ceil(img.width / 8), "big")
                # 6-byte header: row index (2B) + 3 zero counts (3B) + flag (1B)
                header = struct.pack(">H3BB", y, 0, 0, 0, 1)
                await self.client.write_gatt_char(
                    self.char_uuid, build_packet(0x85, header + row_bytes))
                await asyncio.sleep(0.01)

            # 3. endPage — loop until acknowledged
            for _ in range(50):
                _, data = await self._b1_cmd(0xE3, b"\x01")
                if data[0]:
                    break
                await asyncio.sleep(0.05)

            # 4. Poll status until all copies done
            for _ in range(100):
                _, data = await self._b1_cmd(0xA3, b"\x01")
                if len(data) >= 2:
                    page = struct.unpack(">H", data[:2])[0]
                    if page >= quantity:
                        break
                await asyncio.sleep(0.1)

            # 5. endPrint
            await self._b1_cmd(0xF3, b"\x01")

        finally:
            await self._stop_notify()

    async def _b1_cmd(self, cmd, data, timeout=10):
        """Send command and wait for response (persistent notify, write-with-response)."""
        self._notify_event.clear()
        await self.client.write_gatt_char(
            self.char_uuid, build_packet(cmd, data))  # default = with response
        await asyncio.wait_for(self._notify_event.wait(), timeout)
        return parse_response(self._notify_data)
