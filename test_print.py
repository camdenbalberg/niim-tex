"""Test with correct D110_M V4 print sequence."""
import asyncio
import math
import struct
from PIL import Image, ImageOps, ImageDraw
from d110 import _packet, _parse_response
from bleak import BleakClient, BleakScanner

PRINTHEAD_PX = 96

def pixel_counts(row_bytes):
    """Count black pixels in 3 chunks (split mode)."""
    chunk_size = math.ceil(PRINTHEAD_PX / 8 / 3)  # 4 bytes per chunk
    counts = [0, 0, 0]
    for i, b in enumerate(row_bytes):
        chunk = min(i // chunk_size, 2)
        for bit in range(8):
            if b & (0x80 >> bit):
                counts[chunk] += 1
    return counts

async def find_d110():
    found = []
    def cb(dev, _):
        if dev.name and dev.name.upper().startswith("D110"): found.append(dev)
    s = BleakScanner(detection_callback=cb)
    await s.start()
    for _ in range(100):
        if found: break
        await asyncio.sleep(0.1)
    await s.stop()
    return found[0]

async def main():
    dev = await find_d110()
    client = BleakClient(dev.address)
    await client.connect()

    # Find char
    char = None
    for svc in client.services:
        cs = list(svc.characteristics)
        if len(cs) == 1 and "write-without-response" in cs[0].properties:
            char = cs[0].uuid
    print(f"Connected: {dev.name}, char={char}")

    evt = asyncio.Event()
    last_data = [None]
    def on_n(_, data):
        cmd, d = _parse_response(data)
        last_data[0] = data
        print(f"  << 0x{cmd:02X} {d.hex()}")
        evt.set()

    await client.start_notify(char, on_n)

    async def cmd(c, d, t=10):
        evt.clear()
        await client.write_gatt_char(char, _packet(c, d), response=False)
        await asyncio.wait_for(evt.wait(), t)

    async def fire(c, d):
        await client.write_gatt_char(char, _packet(c, d), response=False)

    # Image: 96x50 black rectangle
    src = Image.new("L", (96, 50), 255)
    ImageDraw.Draw(src).rectangle([10, 10, 85, 40], fill=0)
    img = ImageOps.invert(src.convert("L")).convert("1")

    # === D110_M V4 PRINT SEQUENCE ===

    # 1. Init
    print(">> setDensity(3)")
    await cmd(0x21, bytes((3,)))

    print(">> setLabelType(1)")
    await cmd(0x23, bytes((1,)))

    print(">> printStart9b (1 page)")
    await cmd(0x01, struct.pack(">H7B", 1, 0, 0, 0, 0, 0, 0, 0))

    # 2. Page (NO startPage!)
    print(">> throwaway status (one-way)")
    await fire(0xA3, b"\x01")
    await asyncio.sleep(0.2)
    evt.clear()

    # 3. setPageSize13b: rows, cols, copies, cutHeight, cutType, 0, sendAll, partHeight
    rows, cols, copies = img.height, img.width, 1
    page_size = struct.pack(">HHHH3BH", rows, cols, copies, 0, 0, 0, 0, 0)
    print(f">> setPageSize13b ({len(page_size)}b): rows={rows} cols={cols} copies={copies}")
    await cmd(0x13, page_size)

    # 4. Image data with pixel counts
    print(f">> Sending {img.height} image rows...")
    for y in range(img.height):
        bits = "".join("0" if img.getpixel((x, y)) == 0 else "1" for x in range(img.width))
        row_bytes = int(bits, 2).to_bytes(math.ceil(img.width / 8), "big")
        counts = pixel_counts(row_bytes)
        header = struct.pack(">H3BB", y, counts[0], counts[1], counts[2], 1)
        pkt = _packet(0x85, header + row_bytes)
        if y < 2 or y == 10:
            print(f"   row {y}: counts={counts} hex={pkt.hex()}")
        await client.write_gatt_char(char, pkt, response=False)
        await asyncio.sleep(0.01)
    print("   done")
    await asyncio.sleep(0.5)

    # 5. pageEnd
    print(">> pageEnd")
    await cmd(0xE3, b"\x01")

    # 6. Poll status
    print(">> polling print status...")
    for _ in range(50):
        await cmd(0xA3, b"\x01")
        resp = _parse_response(last_data[0])
        page = struct.unpack(">H", resp[1][:2])[0]
        if page >= copies:
            print(f"   page={page}, done!")
            break
        await asyncio.sleep(0.2)

    # 7. endPrint
    print(">> endPrint")
    await cmd(0xF3, b"\x01")

    # BLE workaround: one-way heartbeat after endPrint
    print(">> one-way heartbeat")
    await fire(0xDC, b"\x01")

    await client.stop_notify(char)
    await client.disconnect()
    print("DONE!")

asyncio.run(main())
