#!/usr/bin/env python3
"""Dump raw RFID data from the loaded label roll for protocol analysis."""

import asyncio
import sys
sys.path.insert(0, ".")

from niim_tex.models import get_printer


async def dump():
    p = get_printer(None)
    name = await p.connect()
    print(f"Connected to {name}\n")

    _, data = await p._command(0x1A, b"\x01")

    print(f"Raw length: {len(data)} bytes")
    print(f"Hex: {data.hex()}")
    print(f"Bytes: {list(data)}")
    print()

    # Walk through the known fields
    print("=== Parsed fields ===")
    print(f"[0:8]  UUID:  {data[0:8].hex()}")
    idx = 8

    barcode_len = data[idx]
    idx += 1
    barcode = data[idx:idx + barcode_len]
    print(f"[{idx-1}]   Barcode len: {barcode_len}")
    print(f"[{idx}:{idx+barcode_len}] Barcode: {barcode.decode(errors='replace')} (hex: {barcode.hex()})")
    idx += barcode_len

    serial_len = data[idx]
    idx += 1
    serial = data[idx:idx + serial_len]
    print(f"[{idx-1}]   Serial len: {serial_len}")
    print(f"[{idx}:{idx+serial_len}] Serial: {serial.decode(errors='replace')} (hex: {serial.hex()})")
    idx += serial_len

    print(f"\n=== Remaining bytes after serial (index {idx} to {len(data)-1}) ===")
    remaining = data[idx:]
    print(f"Length: {len(remaining)} bytes")
    print(f"Hex: {remaining.hex()}")
    print(f"Bytes: {list(remaining)}")

    # Try different interpretations of the remaining bytes
    print("\n=== Interpretation attempts ===")
    for i in range(0, len(remaining)):
        if i + 2 <= len(remaining):
            val_be = int.from_bytes(remaining[i:i+2], "big")
            val_le = int.from_bytes(remaining[i:i+2], "little")
            print(f"  [{idx+i}:{idx+i+2}] BE={val_be:5d}  LE={val_le:5d}  raw={remaining[i:i+2].hex()}")

    await p.disconnect()


asyncio.run(dump())
