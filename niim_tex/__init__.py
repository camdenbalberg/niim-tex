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


def parse_rfid_barcode(barcode):
    """Parse an RFID barcode string like 'T15*50-125' or 'T12.5*74+35-60'.

    Returns a dict with:
        tape_width:   float (mm)
        label_length: float (mm)  — for cable labels, this is the flag length
        wrap_length:  float or None (mm) — cable wrap portion
        total_length: float (mm) — full printable length (label + wrap)
        count:        int or None — labels per roll
        is_cable:     bool
        size_key:     str — matching LABEL_SIZES key (e.g. '15x50')
    Returns None if the barcode can't be parsed.
    """
    import re
    # Format: T<width>*<length>[+<wrap>][-<count>][<suffix>]
    m = re.match(
        r"^T([\d.]+)\*([\d.]+)(?:\+([\d.]+))?(?:-([\d]+))?",
        barcode.strip(),
    )
    if not m:
        return None

    tape_w = float(m.group(1))
    label_l = float(m.group(2))
    wrap_l = float(m.group(3)) if m.group(3) else None
    count = int(m.group(4)) if m.group(4) else None
    total_l = label_l + (wrap_l or 0)

    # Find matching size key
    # For cable labels, the primary size is tape_width x label_length (flag)
    def _fmt(w, l):
        ws = f"{w:g}"
        ls = f"{l:g}"
        return f"{ws}x{ls}"

    size_key = _fmt(tape_w, label_l)
    if size_key not in LABEL_SIZES:
        # Try total length for cable labels
        if wrap_l:
            alt = _fmt(tape_w, total_l)
            if alt in LABEL_SIZES:
                size_key = alt

    return {
        "tape_width": tape_w,
        "label_length": label_l,
        "wrap_length": wrap_l,
        "total_length": total_l,
        "count": count,
        "is_cable": wrap_l is not None,
        "size_key": size_key,
    }


def mm_to_px(mm):
    """Convert millimeters to pixels at NIIMBOT DPI (203)."""
    return round(mm * DPI / MM_PER_INCH)
