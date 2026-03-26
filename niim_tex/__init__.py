"""niim-tex: LaTeX-to-NIIMBOT label print pipeline with multi-device BLE support."""

DPI = 203  # NIIMBOT native resolution
MM_PER_INCH = 25.4
PRINTABLE_HEIGHT_MM = 12  # D110 printhead is 12mm (96px)

# (tape_width_mm, label_length_mm)
LABEL_SIZES = {
    "12x22":   (12, 22),
    "12x30":   (12, 30),
    "12x40":   (12, 40),
    "13x35":   (13, 35),
    "14x25":   (14, 25),
    "14x30":   (14, 30),
    "14x40":   (14, 40),
    "14x60":   (14, 60),
    "15x50":   (15, 50),
    "12.5x74": (12.5, 74),  # Cable label (T12.5*74+35): 74mm flag, 35mm cable wrap
}


def mm_to_px(mm):
    """Convert millimeters to pixels at NIIMBOT DPI (203)."""
    return round(mm * DPI / MM_PER_INCH)
