"""niim-tex: LaTeX-to-NIIMBOT label print pipeline with multi-device BLE support."""

DPI = 203  # NIIMBOT native resolution
MM_PER_INCH = 25.4
PRINTABLE_HEIGHT_MM = 12  # D110 printhead is 12mm (96px)

# (tape_width_mm, label_length_mm)
# Covers standard NIIMBOT D-series label rolls
LABEL_SIZES = {
    # 12mm tape
    "12x22":   (12, 22),
    "12x30":   (12, 30),
    "12x40":   (12, 40),
    "12x50":   (12, 50),
    "12x60":   (12, 60),
    "12x70":   (12, 70),
    "12x75":   (12, 75),
    # 13mm tape
    "13x35":   (13, 35),
    # 14mm tape
    "14x22":   (14, 22),
    "14x25":   (14, 25),
    "14x30":   (14, 30),
    "14x40":   (14, 40),
    "14x50":   (14, 50),
    "14x60":   (14, 60),
    "14x70":   (14, 70),
    # 15mm tape
    "15x30":   (15, 30),
    "15x50":   (15, 50),
    "15x70":   (15, 70),
    # Cable label: T12.5*74+35 (74mm flag folds at 37mm, 35mm cable wrap)
    # Template sizes for different use cases:
    "12.5x109": (12.5, 109),  # Full label: 74mm flag + 35mm wrap
    "12.5x74":  (12.5, 74),   # Full flag only (both halves)
    "12.5x37":  (12.5, 37),   # Single flag half (front or back)
    "12.5x35":  (12.5, 35),   # Cable wrap portion only
}

# Cable label roll definition — maps the physical roll to its component regions
CABLE_LABEL = {
    "roll": "T12.5*74+35",
    "tape_width": 12.5,
    "flag_length": 74,       # Total flag (folds in half)
    "half_flag_length": 37,  # Each side of the flag
    "wrap_length": 35,       # Cable wrap portion
    "total_length": 109,     # flag + wrap
    # Template sizes that belong to this roll
    "sizes": ["12.5x109", "12.5x74", "12.5x37", "12.5x35"],
}


def is_cable_size(size_key):
    """Check if a label size key belongs to the cable label roll."""
    return size_key in CABLE_LABEL["sizes"]


# RFID barcode → label roll lookup table.
# The RFID tag stores a product EAN/UPC barcode, not the label model string.
# Map these to (size_key, label_count). The RFID chip stores total * 1.2
# (includes calibration/waste labels), so we track the real count here.
# Add new barcodes as you encounter them — run `niim-tex rfid` to see the barcode.
RFID_BARCODE_DB = {
    "6972842743596": {"size_key": "15x50",   "model": "T15*50-125",      "count": 125},
    "01222281":      {"size_key": "12x40",   "model": "T12*40-155",      "count": 155},
    "6972842743787": {"size_key": "12.5x74", "model": "T12.5*74+35-60",  "count": 60, "is_cable": True},
}

# RFID label counts are inflated by this factor (extra labels for calibration/waste)
RFID_COUNT_FACTOR = 1.2


def _query_niimbot_cloud(barcode):
    """Query the NIIMBOT cloud API to resolve an unknown barcode.

    Returns a dict matching RFID_BARCODE_DB format, or None on failure.
    """
    import json
    import re
    import urllib.request
    import urllib.error

    url = "https://print.niimbot.com/api/template/getCloudTemplateByOneCode"
    headers = {
        "Content-Type": "application/json",
        "niimbot-user-agent": "AppVersionName/999.0.0",
    }
    body = json.dumps({"oneCode": barcode}).encode()

    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    if data.get("code") != 1 or "data" not in data:
        return None

    d = data["data"]
    width = d.get("width")    # label length in mm
    height = d.get("height")  # tape width in mm
    is_cable = d.get("isCable", False)
    cable_len = d.get("cableLength", 0)

    if not width or not height:
        return None

    tape_w = float(height)
    label_l = float(width)

    # Extract model name from English label name
    model = ""
    for n in d.get("labelNames", []):
        if n.get("languageCode") == "en":
            model = n.get("name", "")
            break

    # Parse count from model name (e.g. "T15*50-125WHITE" -> 125)
    count = None
    m = re.search(r"-(\d+)", model)
    if m:
        count = int(m.group(1))

    # Build size key
    size_key = f"{tape_w:g}x{label_l:g}"

    entry = {"size_key": size_key, "model": model, "count": count}
    if is_cable:
        entry["is_cable"] = True
        if cable_len:
            entry["cable_length"] = int(cable_len)

    return entry


def lookup_rfid_barcode(barcode):
    """Look up label info from an RFID barcode string.

    Checks the local DB first, then falls back to the NIIMBOT cloud API.
    Successfully resolved cloud lookups are cached in the local DB for
    future use (within the same process).

    Returns a dict with size_key, model, count, tape_width, label_length, is_cable,
    or None if the barcode can't be resolved.
    """
    entry = RFID_BARCODE_DB.get(barcode)

    # Cloud API fallback
    if not entry:
        entry = _query_niimbot_cloud(barcode)
        if entry:
            # Cache for future lookups in this session
            RFID_BARCODE_DB[barcode] = entry

    if not entry:
        return None

    size_key = entry["size_key"]

    # Get dimensions from LABEL_SIZES if available, otherwise parse from size_key
    if size_key in LABEL_SIZES:
        tape_w, label_l = LABEL_SIZES[size_key]
    else:
        parts = size_key.split("x")
        tape_w = float(parts[0])
        label_l = float(parts[1])

    return {
        "size_key": size_key,
        "model": entry.get("model", ""),
        "count": entry.get("count"),
        "tape_width": tape_w,
        "label_length": label_l,
        "is_cable": entry.get("is_cable", False),
    }


def correct_rfid_count(rfid_total):
    """Convert RFID total count to real label count (removes 1.2x overhead)."""
    return round(rfid_total / RFID_COUNT_FACTOR)


def mm_to_px(mm):
    """Convert millimeters to pixels at NIIMBOT DPI (203)."""
    return round(mm * DPI / MM_PER_INCH)
