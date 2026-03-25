#!/usr/bin/env python3
"""niim-mosaic: Split any image into printable label strips for the NIIMBOT D110."""

import argparse
import asyncio
import os
import sys

from PIL import Image, ImageDraw, ImageOps

from d110 import D110, LabelType

DPI = 203
MM_PER_INCH = 25.4

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
    "12.5x74": (12.5, 74),
}

PRINTABLE_HEIGHT_MM = 12  # D110 printhead is 12mm (96px)


def mm_to_px(mm):
    return round(mm * DPI / MM_PER_INCH)


def prepare_image(path, label_size, dither=True, threshold=128):
    """Load, resize, convert to B&W, and slice into label strips."""
    tape_w, label_l = label_size
    strip_h_px = mm_to_px(PRINTABLE_HEIGHT_MM)  # 96px
    strip_w_px = mm_to_px(label_l)              # e.g. 320px for 40mm

    img = Image.open(path)

    # Resize so width = label length in pixels, preserve aspect ratio
    ratio = strip_w_px / img.width
    new_h = round(img.height * ratio)
    img = img.resize((strip_w_px, new_h), Image.LANCZOS)

    # Pad height to multiple of strip height (white at bottom)
    remainder = new_h % strip_h_px
    if remainder:
        pad = strip_h_px - remainder
        img = ImageOps.expand(img, border=(0, 0, 0, pad), fill=255)

    # Convert to B&W
    img = img.convert("L")
    if dither:
        img = img.convert("1")  # Floyd-Steinberg dithering
    else:
        img = img.point(lambda x: 255 if x > threshold else 0, "1")

    # Slice into horizontal strips
    n_strips = img.height // strip_h_px
    strips = []
    for i in range(n_strips):
        y0 = i * strip_h_px
        strip = img.crop((0, y0, strip_w_px, y0 + strip_h_px))
        strips.append(strip)

    return strips, img


def save_preview(strips, output_path):
    """Save a numbered preview image showing all strips with gap lines."""
    if not strips:
        return
    w = strips[0].width
    h = strips[0].height
    gap = 4
    margin_left = 30
    total_h = len(strips) * h + (len(strips) - 1) * gap
    preview = Image.new("L", (w + margin_left, total_h), 200)
    draw = ImageDraw.Draw(preview)

    for i, strip in enumerate(strips):
        y = i * (h + gap)
        # Convert 1-bit strip to grayscale for pasting
        preview.paste(strip.convert("L"), (margin_left, y))
        # Strip number
        draw.text((2, y + h // 2 - 5), str(i + 1), fill=0)

    preview.save(output_path)
    print(f"Preview saved to {output_path}")


async def print_strips(strips, density=3, start=1):
    """Connect to D110 and print each strip sequentially."""
    printer = D110()
    try:
        name = await printer.connect()
        print(f"Connected to {name}")

        total = len(strips)
        for i in range(start - 1, total):
            strip = strips[i]
            # Rotate 90° CW — landscape strip → portrait for printer feed
            rotated = strip.rotate(-90, expand=True)
            print(f"Printing strip {i + 1}/{total}...")
            await printer.print_image(rotated, density=density)
            print(f"  Strip {i + 1} done.")

        print("All strips printed.")
    finally:
        await printer.disconnect()


def main():
    parser = argparse.ArgumentParser(
        prog="niim-mosaic",
        description="Split an image into printable label strips for the NIIMBOT D110",
    )
    parser.add_argument("image", help="Input image (jpg, png, etc.)")
    parser.add_argument("--size", default="12x40",
                        help=f"Label size (default: 12x40). Options: {', '.join(LABEL_SIZES)}")
    parser.add_argument("--density", type=int, default=3, choices=range(1, 4),
                        metavar="N", help="Print darkness 1-3 (default: 3)")
    parser.add_argument("--dither", action="store_true", default=True,
                        help="Use Floyd-Steinberg dithering (default)")
    parser.add_argument("--no-dither", dest="dither", action="store_false",
                        help="Use hard threshold instead of dithering")
    parser.add_argument("--threshold", type=int, default=128, metavar="N",
                        help="B&W threshold 0-255, used with --no-dither (default: 128)")
    parser.add_argument("--preview-only", action="store_true",
                        help="Generate preview image without printing")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show strip count and dimensions, don't print")
    parser.add_argument("--start", type=int, default=1, metavar="N",
                        help="Start printing from strip N (1-indexed, for reprints)")

    args = parser.parse_args()

    if not os.path.isfile(args.image):
        print(f"Error: file not found: {args.image}", file=sys.stderr)
        sys.exit(1)

    if args.size not in LABEL_SIZES:
        print(f"Error: unknown label size '{args.size}'", file=sys.stderr)
        print(f"Options: {', '.join(LABEL_SIZES)}", file=sys.stderr)
        sys.exit(1)

    label_size = LABEL_SIZES[args.size]
    tape_w, label_l = label_size
    strip_h_px = mm_to_px(PRINTABLE_HEIGHT_MM)
    strip_w_px = mm_to_px(label_l)

    print(f"Label: {args.size} ({tape_w}mm tape, {label_l}mm length)")
    print(f"Strip: {strip_w_px}x{strip_h_px}px ({label_l}mm x {PRINTABLE_HEIGHT_MM}mm)")

    strips, full_img = prepare_image(
        args.image, label_size,
        dither=args.dither, threshold=args.threshold,
    )

    n = len(strips)
    total_h_mm = n * PRINTABLE_HEIGHT_MM
    print(f"Image sliced into {n} strips ({label_l}mm x {total_h_mm}mm assembled)")

    if args.dry_run:
        return

    # Always save preview
    base = os.path.splitext(os.path.basename(args.image))[0]
    preview_path = f"{base}_mosaic_preview.png"
    save_preview(strips, preview_path)

    if args.preview_only:
        return

    if args.start < 1 or args.start > n:
        print(f"Error: --start must be between 1 and {n}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(print_strips(strips, density=args.density, start=args.start))


if __name__ == "__main__":
    main()
