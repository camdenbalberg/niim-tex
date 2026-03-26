"""NIIMBOT D110_M V4 print driver."""

import asyncio
import math
import struct

from PIL import ImageOps

from ..printer import NiimbotPrinter
from ..protocol import LabelType, parse_response


class D110Printer(NiimbotPrinter):
    """Driver for the NIIMBOT D110 / D110-M (V4 protocol)."""

    MODEL_PREFIXES = ["D110"]
    MAX_WIDTH_PX = 240
    MAX_DENSITY = 5

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

    async def print_image(self, image, density=3, quantity=1,
                          label_type=LabelType.WITH_GAPS,
                          vertical_offset=0, horizontal_offset=0):
        """Print a PIL Image using the D110_M V4 print sequence.

        Args:
            image: PIL Image to print.
            density: Print darkness 1-5 (default 3).
            quantity: Number of copies (default 1).
            label_type: LabelType enum (default WITH_GAPS).
            vertical_offset: Pixel rows of blank space to add above image.
            horizontal_offset: Pixel columns to shift image (positive=right, negative=left).
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
                _, data = parse_response(self._notify_data)
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
