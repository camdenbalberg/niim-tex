#!/usr/bin/env python3
"""niim-mosaic: Split any image into printable label strips for NIIMBOT thermal printers."""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageOps

# Register AVIF/HEIF support if pillow-heif is installed
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

from niim_tex import LABEL_SIZES, PRINTABLE_HEIGHT_MM, parse_rfid_barcode, mm_to_px
from niim_tex.models import get_printer


def open_image(path):
    """Open an image, falling back to ImageMagick for unsupported formats."""
    try:
        return Image.open(path)
    except Exception:
        pass

    # Fallback: use ImageMagick to convert to PNG
    if not shutil.which("magick"):
        raise RuntimeError(
            f"Cannot open '{os.path.basename(path)}' — Pillow doesn't support this format "
            "and ImageMagick ('magick') is not on PATH for fallback conversion."
        )

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        result = subprocess.run(
            ["magick", path, tmp.name],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ImageMagick failed to convert '{os.path.basename(path)}': {result.stderr.strip()}"
            )
        print(f"Converted {os.path.basename(path)} via ImageMagick")
        return Image.open(tmp.name)
    except Exception:
        os.unlink(tmp.name)
        raise


def get_output_dir(image_path):
    """Return mosaic/<image_name>/ output directory, creating it if needed."""
    base = os.path.splitext(os.path.basename(image_path))[0]
    out_dir = os.path.join("mosaic", base)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _strip_usage(strip):
    """Return the fraction of black pixels in a 1-bit strip (0.0 = all white, 1.0 = all black)."""
    # In mode "1", getcolors returns [(count, 0), (count, 255)] or subset
    total = strip.width * strip.height
    colors = strip.convert("L").getcolors()
    if not colors:
        return 0.0
    black = sum(count for count, val in colors if val < 128)
    return black / total


def prepare_image(path, label_size, grid_width=1, force_height=None,
                  force_aspect_ratio=None, dither=True, threshold=128,
                  crop_bottom=None, tight_fit=False):
    """Load, resize, convert to B&W, and slice into label grid strips.

    Args:
        crop_bottom: If set, remove the last row if its black pixel usage is
                     below this fraction (e.g. 0.2 = remove if <20% used).
        tight_fit:   Resize image to exactly fill whole rows (no bottom padding).
    """
    tape_w, label_l = label_size
    strip_h_px = mm_to_px(PRINTABLE_HEIGHT_MM)  # 96px per strip row
    strip_w_px = mm_to_px(label_l)

    canvas_w = grid_width * strip_w_px

    img = open_image(path).convert("RGB")

    if force_height is not None:
        canvas_h = force_height * strip_h_px
        if force_aspect_ratio:
            # Stretch to fill exact grid
            img = img.resize((canvas_w, canvas_h), Image.LANCZOS)
        else:
            # Fit within canvas preserving aspect ratio, center on white
            fit_ratio = min(canvas_w / img.width, canvas_h / img.height)
            new_w = round(img.width * fit_ratio)
            new_h = round(img.height * fit_ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
            canvas.paste(img, ((canvas_w - new_w) // 2, (canvas_h - new_h) // 2))
            img = canvas
    else:
        if force_aspect_ratio:
            ar_w, ar_h = force_aspect_ratio
            canvas_h = round(canvas_w * ar_h / ar_w)
            img = img.resize((canvas_w, canvas_h), Image.LANCZOS)
        else:
            ratio = canvas_w / img.width
            canvas_h = round(img.height * ratio)
            img = img.resize((canvas_w, canvas_h), Image.LANCZOS)

        if tight_fit:
            # Resize to exactly fill whole rows — no padding needed
            n_rows = max(1, round(img.height / strip_h_px))
            target_h = n_rows * strip_h_px
            if img.height != target_h:
                print(f"Tight fit: resizing height {img.height}px -> {target_h}px ({n_rows} rows)")
                img = img.resize((canvas_w, target_h), Image.LANCZOS)
        else:
            # Pad height to multiple of strip height (white at bottom)
            remainder = img.height % strip_h_px
            if remainder:
                pad = strip_h_px - remainder
                img = ImageOps.expand(img, border=(0, 0, 0, pad), fill="white")

    # Convert to B&W
    img = img.convert("L")
    if dither:
        img = img.convert("1")  # Floyd-Steinberg dithering
    else:
        img = img.point(lambda x: 255 if x > threshold else 0, "1")

    # Slice into grid (row-major order)
    n_rows = img.height // strip_h_px
    strips = []
    for row in range(n_rows):
        for col in range(grid_width):
            x0 = col * strip_w_px
            y0 = row * strip_h_px
            strip = img.crop((x0, y0, x0 + strip_w_px, y0 + strip_h_px))
            strips.append(strip)

    # --crop-bottom: remove last row if usage is below threshold
    if crop_bottom is not None and n_rows > 1:
        last_row_strips = strips[-(grid_width):]
        avg_usage = sum(_strip_usage(s) for s in last_row_strips) / len(last_row_strips)
        if avg_usage < crop_bottom:
            print(f"Cropping bottom row: {avg_usage:.1%} used (threshold: {crop_bottom:.0%})")
            strips = strips[:-(grid_width)]
            n_rows -= 1
            # Crop the full image too for consistent preview
            img = img.crop((0, 0, img.width, n_rows * strip_h_px))

    return strips, img, (n_rows, grid_width)


def save_strips(strips, out_dir, ext):
    """Save individual strip images to the output directory."""
    for i, strip in enumerate(strips):
        strip_path = os.path.join(out_dir, f"strip{i + 1}{ext}")
        strip.save(strip_path)
    print(f"Saved {len(strips)} strips to {out_dir}/")


def save_preview(strips, output_path, grid_shape=None):
    """Save a numbered preview image showing all strips arranged in grid."""
    if not strips:
        return
    w = strips[0].width
    h = strips[0].height
    gap = 4
    margin_left = 30

    if grid_shape and grid_shape[1] > 1:
        n_rows, n_cols = grid_shape
        margin_top = 16
        total_w = margin_left + n_cols * w + (n_cols - 1) * gap
        total_h = margin_top + n_rows * h + (n_rows - 1) * gap
        preview = Image.new("L", (total_w, total_h), 200)
        draw = ImageDraw.Draw(preview)

        # Column headers
        for col in range(n_cols):
            x = margin_left + col * (w + gap) + w // 2 - 3
            draw.text((x, 1), f"c{col + 1}", fill=0)

        for idx, strip in enumerate(strips):
            row = idx // n_cols
            col = idx % n_cols
            x = margin_left + col * (w + gap)
            y = margin_top + row * (h + gap)
            preview.paste(strip.convert("L"), (x, y))
            if col == 0:
                draw.text((2, y + h // 2 - 5), str(row + 1), fill=0)
            # Sequential strip number
            draw.text((x + 2, y + 2), f"#{idx + 1}", fill=128)
    else:
        n_strips = len(strips)
        total_h = n_strips * h + (n_strips - 1) * gap
        preview = Image.new("L", (w + margin_left, total_h), 200)
        draw = ImageDraw.Draw(preview)
        for i, strip in enumerate(strips):
            y = i * (h + gap)
            preview.paste(strip.convert("L"), (margin_left, y))
            draw.text((2, y + h // 2 - 5), str(i + 1), fill=0)

    preview.save(output_path)
    print(f"Preview saved to {output_path}")


async def print_strips(strip_paths, density=3, model=None, expected_size=None):
    """Connect to printer and print the given strip image files."""
    printer = get_printer(model)
    try:
        name = await printer.connect()
        print(f"Connected to {name}")

        # Auto-detect roll from RFID and validate against --size
        try:
            rfid = await printer.get_rfid()
            if rfid and rfid.get("barcode"):
                parsed = parse_rfid_barcode(rfid["barcode"])
                if parsed:
                    detected = parsed["size_key"]
                    print(f"RFID detected: {rfid['barcode']} -> {detected} "
                          f"({parsed['tape_width']}x{parsed['label_length']}mm, "
                          f"{rfid['remaining_labels']} remaining)")
                    if expected_size and expected_size != detected:
                        tape_w_exp, label_l_exp = LABEL_SIZES[expected_size]
                        print(f"Error: --size is {expected_size} "
                              f"({tape_w_exp}x{label_l_exp}mm) but loaded roll "
                              f"is {detected} ({parsed['tape_width']}x{parsed['label_length']}mm)")
                        print("Use --size matching the loaded roll, or swap the roll.")
                        return
        except Exception:
            pass  # RFID read failed, continue without validation

        total = len(strip_paths)
        for idx, path in enumerate(strip_paths):
            strip = Image.open(path)
            # Rotate 90° CW — landscape strip → portrait for printer feed
            rotated = strip.rotate(-90, expand=True)
            strip_num = os.path.basename(path)
            print(f"Printing {strip_num} ({idx + 1}/{total})...")
            await printer.print_image(rotated, density=density)
            print(f"  {strip_num} done.")

            # Wait for printer to be ready before next print
            if idx < total - 1:
                await printer.wait_ready()

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
        description="Split an image into printable label strips for NIIMBOT printers",
    )
    parser.add_argument("image", help="Input image (jpg, png, etc.)")
    parser.add_argument("--size", default="12x40",
                        help=f"Label size - must match loaded roll (default: 12x40). Options: {', '.join(LABEL_SIZES)}")
    parser.add_argument("--density", type=int, default=3, choices=range(1, 6),
                        metavar="N", help="Print darkness 1-5 (default: 3)")
    parser.add_argument("--model", type=str, default=None,
                        help="Printer model (e.g. d110). Auto-detects if omitted.")
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
    parser.add_argument("--width", type=int, default=1, metavar="M",
                        help="Labels wide (default: 1). Height auto-calculated from aspect ratio")
    parser.add_argument("--force-height", type=int, default=None, metavar="N",
                        help="Force number of label rows (may leave unused label width)")
    parser.add_argument("--force-aspect-ratio", type=str, default=None, metavar="W:H",
                        help="Force output aspect ratio (e.g. 16:9, 1:1). Stretches image to fit")
    parser.add_argument("--crop-bottom", type=float, nargs="?", const=0.2, default=None,
                        metavar="PCT",
                        help="Remove last row if below usage threshold (default: 0.2 = 20%%)")
    parser.add_argument("--tight-fit", action="store_true",
                        help="Resize image to exactly fill whole rows (no bottom whitespace padding)")

    args = parser.parse_args()

    if not os.path.isfile(args.image):
        print(f"Error: file not found: {args.image}", file=sys.stderr)
        sys.exit(1)

    if args.size not in LABEL_SIZES:
        print(f"Error: unknown label size '{args.size}'", file=sys.stderr)
        print(f"Options: {', '.join(LABEL_SIZES)}", file=sys.stderr)
        sys.exit(1)

    if args.width < 1:
        print("Error: --width must be >= 1", file=sys.stderr)
        sys.exit(1)

    if args.force_height is not None and args.force_height < 1:
        print("Error: --force-height must be >= 1", file=sys.stderr)
        sys.exit(1)

    force_ar = None
    if args.force_aspect_ratio:
        parts = args.force_aspect_ratio.split(":")
        if len(parts) != 2:
            print("Error: --force-aspect-ratio must be W:H (e.g. 16:9)", file=sys.stderr)
            sys.exit(1)
        try:
            force_ar = (float(parts[0]), float(parts[1]))
        except ValueError:
            print("Error: --force-aspect-ratio values must be numbers", file=sys.stderr)
            sys.exit(1)
        if force_ar[0] <= 0 or force_ar[1] <= 0:
            print("Error: --force-aspect-ratio values must be positive", file=sys.stderr)
            sys.exit(1)

    label_size = LABEL_SIZES[args.size]
    tape_w, label_l = label_size
    strip_h_px = mm_to_px(PRINTABLE_HEIGHT_MM)
    strip_w_px = mm_to_px(label_l)

    print(f"Label: {args.size} ({tape_w}mm tape, {label_l}mm length)")
    print(f"Strip: {strip_w_px}x{strip_h_px}px ({label_l}mm x {PRINTABLE_HEIGHT_MM}mm)")
    if args.width > 1:
        print(f"Grid width: {args.width} labels")

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
            asyncio.run(print_strips(paths, density=args.density, model=args.model, expected_size=args.size))
            return

    strips, full_img, grid_shape = prepare_image(
        args.image, label_size,
        grid_width=args.width,
        force_height=args.force_height,
        force_aspect_ratio=force_ar,
        dither=args.dither, threshold=args.threshold,
        crop_bottom=args.crop_bottom,
        tight_fit=args.tight_fit,
    )

    n_rows, n_cols = grid_shape
    n = len(strips)
    assembled_w_mm = n_cols * label_l
    assembled_h_mm = n_rows * PRINTABLE_HEIGHT_MM
    print(f"Image sliced into {n_rows}x{n_cols} grid ({n} labels, {assembled_w_mm}mm x {assembled_h_mm}mm assembled)")

    # Show grid map for multi-column layouts
    if n_cols > 1:
        for row in range(n_rows):
            row_labels = [f"#{row * n_cols + col + 1}" for col in range(n_cols)]
            print(f"  Row {row + 1}: {' | '.join(row_labels)}")

    if args.dry_run:
        return

    # Save individual strips and preview
    save_strips(strips, out_dir, ext)
    save_preview(strips, os.path.join(out_dir, "preview.png"), grid_shape)

    if args.preview_only:
        return

    # Determine which strips to print
    if args.strips:
        selected = parse_strip_list(args.strips, n)
    else:
        selected = list(range(1, n + 1))

    paths = [os.path.join(out_dir, f"strip{s}{ext}") for s in selected]
    asyncio.run(print_strips(paths, density=args.density, model=args.model))


if __name__ == "__main__":
    main()
