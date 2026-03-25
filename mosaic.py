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


def get_output_dir(image_path):
    """Return mosaic/<image_name>/ output directory, creating it if needed."""
    base = os.path.splitext(os.path.basename(image_path))[0]
    out_dir = os.path.join("mosaic", base)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


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


def save_strips(strips, out_dir, ext):
    """Save individual strip images to the output directory."""
    for i, strip in enumerate(strips):
        strip_path = os.path.join(out_dir, f"strip{i + 1}{ext}")
        strip.save(strip_path)
    print(f"Saved {len(strips)} strips to {out_dir}/")


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
        preview.paste(strip.convert("L"), (margin_left, y))
        draw.text((2, y + h // 2 - 5), str(i + 1), fill=0)

    preview.save(output_path)
    print(f"Preview saved to {output_path}")


async def print_strips(strip_paths, density=3):
    """Connect to D110 and print the given strip image files."""
    printer = D110()
    try:
        name = await printer.connect()
        print(f"Connected to {name}")

        total = len(strip_paths)
        for idx, path in enumerate(strip_paths):
            strip = Image.open(path)
            # Rotate 90° CW — landscape strip → portrait for printer feed
            rotated = strip.rotate(-90, expand=True)
            strip_num = os.path.basename(path)
            print(f"Printing {strip_num} ({idx + 1}/{total})...")
            await printer.print_image(rotated, density=density)
            print(f"  {strip_num} done.")

            # Wait for printer to advance past the label gap before next job
            if idx < total - 1:
                await asyncio.sleep(2)

        print("All strips printed.")
    finally:
        await printer.disconnect()


def parse_strip_list(spec, n_strips):
    """Parse a comma-separated strip list like '3,5' into sorted indices."""
    indices = []
    for part in spec.split(","):
        part = part.strip()
        num = int(part)
        if num < 1 or num > n_strips:
            print(f"Error: strip {num} out of range (1-{n_strips})", file=sys.stderr)
            sys.exit(1)
        indices.append(num)
    return sorted(set(indices))


def main():
    parser = argparse.ArgumentParser(
        prog="niim-mosaic",
        description="Split an image into printable label strips for the NIIMBOT D110",
    )
    parser.add_argument("image", help="Input image (jpg, png, etc.)")
    parser.add_argument("--size", default="12x40",
                        help=f"Label size (default: 12x40). Options: {', '.join(LABEL_SIZES)}")
    parser.add_argument("--density", type=int, default=3, choices=range(1, 6),
                        metavar="N", help="Print darkness 1-5 (default: 3)")
    parser.add_argument("--dither", action="store_true", default=True,
                        help="Use Floyd-Steinberg dithering (default)")
    parser.add_argument("--no-dither", dest="dither", action="store_false",
                        help="Use hard threshold instead of dithering")
    parser.add_argument("--threshold", type=int, default=128, metavar="N",
                        help="B&W threshold 0-255, used with --no-dither (default: 128)")
    parser.add_argument("--preview-only", action="store_true",
                        help="Generate strips and preview without printing")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show strip count and dimensions, don't generate or print")
    parser.add_argument("--strips", type=str, metavar="LIST",
                        help="Print specific strips only, e.g. '3,5' or '2'")

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

    # Check if strips already exist on disk (for --strips reprints)
    out_dir = get_output_dir(args.image)
    ext = os.path.splitext(args.image)[1] or ".png"

    # If just reprinting specific strips from existing output, skip processing
    if args.strips and not args.preview_only and not args.dry_run:
        existing = [f for f in os.listdir(out_dir) if f.startswith("strip") and not f.startswith("strip0")]
        if existing:
            # Figure out total strip count from existing files
            nums = [int(f.replace("strip", "").split(".")[0]) for f in existing]
            n_total = max(nums)
            selected = parse_strip_list(args.strips, n_total)
            paths = [os.path.join(out_dir, f"strip{n}{ext}") for n in selected]
            # Verify all requested strips exist
            for p in paths:
                if not os.path.isfile(p):
                    print(f"Error: {p} not found. Re-run without --strips to regenerate.", file=sys.stderr)
                    sys.exit(1)
            print(f"Reprinting strips {', '.join(str(s) for s in selected)} from {out_dir}/")
            asyncio.run(print_strips(paths, density=args.density))
            return

    strips, full_img = prepare_image(
        args.image, label_size,
        dither=args.dither, threshold=args.threshold,
    )

    n = len(strips)
    total_h_mm = n * PRINTABLE_HEIGHT_MM
    print(f"Image sliced into {n} strips ({label_l}mm x {total_h_mm}mm assembled)")

    if args.dry_run:
        return

    # Save individual strips and preview
    save_strips(strips, out_dir, ext)
    save_preview(strips, os.path.join(out_dir, "preview.png"))

    if args.preview_only:
        return

    # Determine which strips to print
    if args.strips:
        selected = parse_strip_list(args.strips, n)
    else:
        selected = list(range(1, n + 1))

    paths = [os.path.join(out_dir, f"strip{s}{ext}") for s in selected]
    asyncio.run(print_strips(paths, density=args.density))


if __name__ == "__main__":
    main()
