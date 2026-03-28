#!/usr/bin/env python3
"""niim-tex: LaTeX-to-NIIMBOT label print pipeline."""

import argparse
import asyncio
import math
import os
import re
import shutil
import subprocess
import sys

from PIL import Image

from niim_tex import DPI, MM_PER_INCH, LABEL_SIZES, CABLE_LABEL, PRINTABLE_HEIGHT_MM, is_cable_size, parse_rfid_barcode, mm_to_px
from niim_tex.protocol import LabelType, SoundType
from niim_tex.models import get_printer


def list_sizes():
    print(f"{'Size':>10}  {'Width':>6}  {'Length':>7}  {'W (px)':>6}  {'L (px)':>7}  {'Note'}")
    print(f"{'':->10}  {'':->6}  {'':->7}  {'':->6}  {'':->7}  {'':->20}")
    for name, (w, l) in LABEL_SIZES.items():
        if is_cable_size(name):
            notes = {
                "12.5x109": "cable: full label (flag + wrap)",
                "12.5x74":  "cable: full flag (both halves)",
                "12.5x37":  "cable: single flag half",
                "12.5x35":  "cable: wrap portion",
            }
            note = notes.get(name, "cable label")
        else:
            note = ""
        print(f"{name:>10}  {w:>5}mm  {l:>5}mm  {mm_to_px(w):>6}  {mm_to_px(l):>7}  {note}")


def generate_tex(size_key, output_name=None):
    if size_key not in LABEL_SIZES:
        print(f"Error: unknown label size '{size_key}'", file=sys.stderr)
        print(f"Run with --list to see available sizes.", file=sys.stderr)
        sys.exit(1)

    tape_w, label_l = LABEL_SIZES[size_key]
    printable_h = min(tape_w, 12)  # D110 printhead is 12mm (96px at 203 DPI)

    # Landscape: long axis = paperwidth, short axis = printable height
    pw = label_l
    ph = printable_h

    filename = output_name if output_name else f"label_{size_key}.tex"
    if not filename.endswith(".tex"):
        filename += ".tex"

    cable_comment = ""
    if is_cable_size(size_key):
        cable_comments = {
            "12.5x109": (
                "% NOTE: Cable label (T12.5*74+35) - FULL label.\n"
                "%   - 0-37mm: front flag half (visible when folded)\n"
                "%   - 37-74mm: back flag half (visible when folded)\n"
                "%   - 74-109mm: cable wrap (wraps around cable)\n"
                "%   - Use 'niim-tex cable' to assemble from halves instead.\n"
            ),
            "12.5x74": (
                "% NOTE: Cable label (T12.5*74+35) - full flag portion.\n"
                "%   - The 74mm flag folds in half at the 37mm midpoint.\n"
                "%   - 0-37mm: front half, 37-74mm: back half.\n"
                "%   - Both halves stick together (non-sticky flag).\n"
                "%   - Use 'niim-tex cable' to assemble from halves instead.\n"
            ),
            "12.5x37": (
                "% NOTE: Cable label (T12.5*74+35) - single flag half.\n"
                "%   - This is one side of the flag (front or back).\n"
                "%   - Use 'niim-tex cable --front this.tex' to auto-duplicate,\n"
                "%     or --front a.tex --back b.tex for different sides.\n"
            ),
            "12.5x35": (
                "% NOTE: Cable label (T12.5*74+35) - cable wrap portion.\n"
                "%   - This wraps around the cable and sticks to itself.\n"
                "%   - Printed text will be visible on the cable.\n"
            ),
        }
        cable_comment = cable_comments.get(size_key, "")

    lines = [
        f"\\documentclass[10pt]{{article}}",
        f"\\usepackage[paperwidth={pw}mm,paperheight={ph}mm,margin=0mm]{{geometry}}",
        f"\\usepackage{{tikz}}",
        f"\\pagestyle{{empty}}",
        f"\\topskip=0pt",
        f"\\parindent=0pt",
        f"",
        f"\\begin{{document}}",
        f"\\noindent",
        f"\\begin{{tikzpicture}}[x=1mm,y=1mm]",
        f"  \\useasboundingbox (0,0) rectangle ({pw},{ph});",
        f"  % Label: {size_key} ({tape_w}mm tape x {label_l}mm length, {ph}mm printable height)",
        f"  % Canvas: (0,0) to ({pw},{ph})",
        f"  % Print:  python niim_tex.py print {filename}",
        f"  %",
    ]
    if cable_comment:
        lines.append(cable_comment.rstrip())
    lines.extend([
        f"  % --- Your content below ---",
        f"",
        f"  \\node[anchor=center] at ({pw/2},{ph/2}) {{Hello}};",
        f"",
        f"\\end{{tikzpicture}}",
        f"\\end{{document}}",
    ])
    content = "\n".join(lines) + "\n"

    with open(filename, "w") as f:
        f.write(content)

    print(f"Created {filename} ({size_key}: {pw}mm x {ph}mm landscape)")


def interactive_new(output_name=None):
    sizes = list(LABEL_SIZES.keys())
    print("Select a label size:")
    for i, name in enumerate(sizes, 1):
        w, l = LABEL_SIZES[name]
        extra = " (cable)" if is_cable_size(name) else ""
        print(f"  {i}) {name}  ({w}mm x {l}mm){extra}")

    while True:
        try:
            choice = input("\nEnter number: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(sizes):
                break
            print(f"Please enter 1-{len(sizes)}.")
        except (ValueError, EOFError):
            print(f"Please enter 1-{len(sizes)}.")

    generate_tex(sizes[idx], output_name)


def parse_geometry_from_tex(tex_path):
    """Extract paperwidth and paperheight from the geometry package line."""
    with open(tex_path, "r") as f:
        content = f.read()

    m = re.search(
        r"paperwidth\s*=\s*([\d.]+)\s*mm.*?paperheight\s*=\s*([\d.]+)\s*mm",
        content,
    )
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def find_label_size_for_geometry(pw, ph):
    """Given geometry (paperwidth, paperheight), find the matching label size.
    Templates are landscape: pw=label_length, ph=tape_width."""
    for name, (tape_w, label_l) in LABEL_SIZES.items():
        if math.isclose(pw, label_l, abs_tol=0.5) and math.isclose(ph, tape_w, abs_tol=0.5):
            return name, tape_w, label_l
    return None, ph, pw  # unknown size, return as-is (ph=tape_w, pw=label_l)


def compile_tex_to_png(tex_path, build_dir=None):
    """Compile a .tex file to PNG. Returns (png_path, build_dir)."""
    tex_path = os.path.abspath(tex_path)
    base_name = os.path.splitext(os.path.basename(tex_path))[0]

    if build_dir is None:
        build_dir = os.path.join(os.getcwd(), "builds", base_name)
    os.makedirs(build_dir, exist_ok=True)
    pdf_path = os.path.join(build_dir, base_name + ".pdf")
    png_path = os.path.join(build_dir, base_name + ".png")

    print(f"Compiling {os.path.basename(tex_path)}...")
    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
         f"-output-directory={build_dir}", tex_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("pdflatex failed:", file=sys.stderr)
        lines = result.stdout.strip().split("\n")
        for line in lines[-20:]:
            print(f"  {line}", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(pdf_path):
        print(f"Error: pdflatex did not produce PDF", file=sys.stderr)
        sys.exit(1)

    print(f"Converting to PNG at {DPI} DPI...")
    result = subprocess.run(
        ["magick", "-density", str(DPI), pdf_path,
         "-rotate", "90",
         "-colorspace", "Gray", "-depth", "8",
         png_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("ImageMagick conversion failed:", file=sys.stderr)
        print(f"  {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    return png_path, build_dir


def run_cable(front_path, back_path=None, wrap_path=None,
              density=3, quantity=1, label_type=1, model=None, preview_only=False):
    """Assemble and print a cable label from half-flag / flag / wrap components."""
    # Check tools
    for tool, hint in [("pdflatex", "Install a TeX distribution (e.g. MiKTeX or TeX Live)"),
                       ("magick", "Install ImageMagick and add to PATH")]:
        if not shutil.which(tool):
            print(f"Error: '{tool}' not found in PATH. {hint}", file=sys.stderr)
            sys.exit(1)

    # Compile front
    if not os.path.isfile(front_path):
        print(f"Error: file not found: {front_path}", file=sys.stderr)
        sys.exit(1)

    build_dir = os.path.join(os.getcwd(), "builds", "cable_label")
    os.makedirs(build_dir, exist_ok=True)

    front_png, _ = compile_tex_to_png(front_path, build_dir)
    front_img = Image.open(front_png)

    # Detect if front is a full flag (74mm) or half flag (37mm)
    front_pw, front_ph = parse_geometry_from_tex(front_path)
    half_flag_h = mm_to_px(CABLE_LABEL["half_flag_length"])  # 296px
    full_flag_h = mm_to_px(CABLE_LABEL["flag_length"])       # 592px
    full_label_h = mm_to_px(CABLE_LABEL["total_length"])     # 872px
    flag_w = mm_to_px(CABLE_LABEL["tape_width"])             # 100px (but print at 96px)
    printable_w = mm_to_px(PRINTABLE_HEIGHT_MM)              # 96px

    # After rotate 90, image is portrait: width=tape, height=length
    img_h = front_img.height
    img_w = front_img.width
    tolerance = 10  # pixels

    if back_path:
        # Two separate halves → combine
        if not os.path.isfile(back_path):
            print(f"Error: file not found: {back_path}", file=sys.stderr)
            sys.exit(1)
        back_png, _ = compile_tex_to_png(back_path, build_dir)
        back_img = Image.open(back_png)

        print("Assembling cable label: front + back halves")
        # Stack vertically: front on top, back on bottom (after rotation both are portrait)
        flag_img = Image.new("L", (img_w, img_h + back_img.height), 255)
        flag_img.paste(front_img, (0, 0))
        flag_img.paste(back_img, (0, img_h))
    elif abs(img_h - half_flag_h) < tolerance:
        # Single half → duplicate for both sides
        print("Assembling cable label: duplicating front half for back")
        flag_img = Image.new("L", (img_w, img_h * 2), 255)
        flag_img.paste(front_img, (0, 0))
        flag_img.paste(front_img, (0, img_h))
    elif abs(img_h - full_flag_h) < tolerance:
        # Already a full flag
        print("Cable label: full flag provided")
        flag_img = front_img
    elif abs(img_h - full_label_h) < tolerance:
        # Full label (flag + wrap)
        print("Cable label: full label provided (flag + wrap)")
        flag_img = front_img
    else:
        print(f"Warning: front image is {img_h}px tall, expected ~{half_flag_h}px (half) "
              f"or ~{full_flag_h}px (full flag) or ~{full_label_h}px (full label)")
        flag_img = front_img

    # Optionally append wrap
    if wrap_path:
        if not os.path.isfile(wrap_path):
            print(f"Error: file not found: {wrap_path}", file=sys.stderr)
            sys.exit(1)
        wrap_png, _ = compile_tex_to_png(wrap_path, build_dir)
        wrap_img = Image.open(wrap_png)
        print(f"Appending cable wrap ({wrap_img.height}px)")
        combined = Image.new("L", (flag_img.width, flag_img.height + wrap_img.height), 255)
        combined.paste(flag_img, (0, 0))
        combined.paste(wrap_img, (0, flag_img.height))
        flag_img = combined

    # Save assembled preview
    preview_path = os.path.join(build_dir, "cable_assembled.png")
    flag_img.save(preview_path)
    print(f"Assembled cable label: {flag_img.width}x{flag_img.height}px")
    print(f"Preview saved to {preview_path}")

    if preview_only:
        return

    # Print
    print("Sending to printer...")
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        asyncio.run(printer.print_image(
            flag_img,
            density=density,
            quantity=quantity,
            label_type=label_type,
        ))
        print("Print job completed.")
    except Exception as e:
        print(f"Print failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())

    print("Done.")


def validate_roll(roll_size, actual_w, actual_h):
    """Validate image dimensions against the specified roll size.
    Returns True if valid, prints error and returns False if not."""
    if roll_size not in LABEL_SIZES:
        print(f"Error: unknown roll size '{roll_size}'", file=sys.stderr)
        print(f"Options: {', '.join(LABEL_SIZES)}", file=sys.stderr)
        return False

    tape_w, label_l = LABEL_SIZES[roll_size]
    # After 90° rotation, image is portrait: width=tape_w, height=label_l
    expected_w = mm_to_px(min(tape_w, PRINTABLE_HEIGHT_MM))
    expected_h = mm_to_px(label_l)
    tolerance = 10  # pixels

    if abs(actual_w - expected_w) > tolerance or abs(actual_h - expected_h) > tolerance:
        print(f"Error: image is {actual_w}x{actual_h}px but roll '{roll_size}' "
              f"expects ~{expected_w}x{expected_h}px", file=sys.stderr)
        print(f"  Image:    {actual_w}x{actual_h}px "
              f"({actual_w * MM_PER_INCH / DPI:.1f}mm x {actual_h * MM_PER_INCH / DPI:.1f}mm)",
              file=sys.stderr)
        print(f"  Roll:     {expected_w}x{expected_h}px "
              f"({min(tape_w, PRINTABLE_HEIGHT_MM)}mm x {label_l}mm)",
              file=sys.stderr)
        if is_cable_size(roll_size):
            print(f"  Hint: cable labels have multiple template sizes. "
                  f"Run --list to see options, or use 'cable' subcommand.", file=sys.stderr)
        return False
    return True


def run_print(tex_path, density=3, rotate=0, quantity=1, label_type=1, model=None, roll=None, fit=False, no_stretch=False):
    if not os.path.isfile(tex_path):
        print(f"Error: file not found: {tex_path}", file=sys.stderr)
        sys.exit(1)

    if not tex_path.endswith(".tex"):
        print(f"Error: expected a .tex file, got: {tex_path}", file=sys.stderr)
        sys.exit(1)

    # Check tools
    for tool, hint in [("pdflatex", "Install a TeX distribution (e.g. MiKTeX or TeX Live)"),
                       ("magick", "Install ImageMagick and add to PATH")]:
        if not shutil.which(tool):
            print(f"Error: '{tool}' not found in PATH. {hint}", file=sys.stderr)
            sys.exit(1)

    tex_path = os.path.abspath(tex_path)
    base_name = os.path.splitext(os.path.basename(tex_path))[0]

    # Parse geometry for sanity checking
    pw, ph = parse_geometry_from_tex(tex_path)
    size_name, tape_w, label_l = None, None, None
    if pw and ph:
        size_name, tape_w, label_l = find_label_size_for_geometry(pw, ph)

    # Use compile_tex_to_png helper
    png_path, build_dir = compile_tex_to_png(tex_path)

    # Step 3: Sanity check dimensions
    img = Image.open(png_path)
    actual_w, actual_h = img.width, img.height

    # --fit: resize image to match label dimensions
    if fit:
        # Determine target from --roll, geometry, or fail
        fit_size = roll or size_name
        if fit_size and fit_size in LABEL_SIZES:
            fit_tw, fit_ll = LABEL_SIZES[fit_size]
            target_w = mm_to_px(min(fit_tw, PRINTABLE_HEIGHT_MM))
            target_h = mm_to_px(fit_ll)
            if actual_w != target_w or actual_h != target_h:
                if no_stretch:
                    # Preserve aspect ratio: scale to fit within target, center
                    scale = min(target_w / actual_w, target_h / actual_h)
                    new_w = round(actual_w * scale)
                    new_h = round(actual_h * scale)
                    print(f"Fitting image (no-stretch): {actual_w}x{actual_h}px -> {new_w}x{new_h}px (centered in {target_w}x{target_h}px)")
                    resized = img.resize((new_w, new_h), Image.LANCZOS)
                    canvas = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 0))
                    paste_x = (target_w - new_w) // 2
                    paste_y = (target_h - new_h) // 2
                    canvas.paste(resized, (paste_x, paste_y))
                    img = canvas.convert("L")
                else:
                    print(f"Fitting image: {actual_w}x{actual_h}px -> {target_w}x{target_h}px")
                    img = img.resize((target_w, target_h), Image.LANCZOS)
                actual_w, actual_h = target_w, target_h
        else:
            print("Warning: --fit requires --roll or a recognized .tex geometry to know the target size",
                  file=sys.stderr)

    # Validate against --roll if specified (hard error)
    if roll:
        if not validate_roll(roll, actual_w, actual_h):
            sys.exit(1)
        print(f"Roll validation OK: image matches {roll}")
    elif tape_w and label_l:
        # Soft warning from geometry parsing
        expected_w = mm_to_px(tape_w)
        expected_h = mm_to_px(label_l)
        tolerance = 5  # pixels
        if abs(actual_w - expected_w) > tolerance or abs(actual_h - expected_h) > tolerance:
            print(f"Warning: image is {actual_w}x{actual_h}px, "
                  f"expected ~{expected_w}x{expected_h}px for {size_name or f'{tape_w}x{label_l}mm'}")

    print(f"Build output saved to {build_dir}/")

    # Step 4: Send to printer
    print("Sending to printer...")

    if rotate != 0:
        img = img.rotate(-rotate, expand=True)

    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")

        # Auto-detect roll from RFID if --roll not specified
        if not roll:
            try:
                rfid = asyncio.run(printer.get_rfid())
                if rfid and rfid.get("barcode"):
                    parsed = parse_rfid_barcode(rfid["barcode"])
                    if parsed:
                        roll = parsed["size_key"]
                        print(f"RFID detected: {rfid['barcode']} -> {roll} "
                              f"({parsed['tape_width']}x{parsed['label_length']}mm, "
                              f"{rfid['remaining_labels']} remaining)")
            except Exception:
                pass  # RFID read failed, continue without validation

        # Validate image against detected/specified roll
        if roll:
            if not validate_roll(roll, actual_w, actual_h):
                sys.exit(1)
            print(f"Roll validation OK: image matches {roll}")

        asyncio.run(printer.print_image(
            img,
            density=density,
            quantity=quantity,
            label_type=label_type,
        ))
        print("Print job completed.")
    except Exception as e:
        print(f"Print failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())

    print("Done.")


def run_info(model=None):
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        info = asyncio.run(printer.get_info())
        print(f"  Serial:        {info['serial']}")
        print(f"  Software:      {info['software']}")
        print(f"  Hardware:      {info['hardware']}")
        print(f"  Battery:       {info['battery']}%")
        print(f"  Density:       {info['density']}")
        print(f"  Speed:         {info['speed']}")
        print(f"  Label type:    {info['label_type']}")
        print(f"  Device type:   {info['device_type']}")
        if "bluetooth_address" in info:
            print(f"  BT address:    {info['bluetooth_address']}")
        if "auto_shutdown_time" in info:
            print(f"  Auto shutdown: {info['auto_shutdown_time']}")
        if "language" in info:
            print(f"  Language:      {info['language']}")
        if "print_mode" in info:
            print(f"  Print mode:    {info['print_mode']}")
        if "area" in info:
            print(f"  Area:          {info['area']}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def run_rfid(model=None):
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        rfid = asyncio.run(printer.get_rfid())
        if rfid is None:
            print("No RFID tag detected on label roll.")
        else:
            print(f"  UUID:       {rfid['uuid']}")
            print(f"  Barcode:    {rfid['barcode']}")
            print(f"  Serial:     {rfid['serial']}")
            print(f"  Total:      {rfid['total_labels']} labels")
            print(f"  Used:       {rfid['used_labels']} labels")
            print(f"  Remaining:  {rfid['remaining_labels']} labels")
            print(f"  Type:       {rfid['type']}")

            # Parse label dimensions from barcode
            parsed = parse_rfid_barcode(rfid["barcode"])
            if parsed:
                print(f"\n  Detected label:")
                print(f"    Tape width:    {parsed['tape_width']}mm")
                print(f"    Label length:  {parsed['label_length']}mm")
                if parsed["is_cable"]:
                    print(f"    Wrap length:   {parsed['wrap_length']}mm")
                    print(f"    Total length:  {parsed['total_length']}mm")
                    print(f"    Type:          cable label")
                if parsed["size_key"] in LABEL_SIZES:
                    print(f"    Size key:      {parsed['size_key']}")
                else:
                    print(f"    Size key:      {parsed['size_key']} (not in LABEL_SIZES)")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def run_heartbeat(model=None):
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        hb = asyncio.run(printer.heartbeat())
        if hb["closing_state"] is not None:
            print(f"  Lid:    {'closed' if hb['closing_state'] == 0 else 'OPEN'}")
        if hb["power_level"] is not None:
            print(f"  Power:  level {hb['power_level']}")
        if hb["paper_state"] is not None:
            print(f"  Paper:  {'inserted' if hb['paper_state'] == 0 else 'NO PAPER'}")
        if hb["rfid_read_state"] is not None:
            print(f"  RFID:   {'ok' if hb['rfid_read_state'] else 'not read'}")
        print(f"  Raw:    {hb['raw']}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def run_feed(model=None):
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        print("Feeding paper for label calibration...")
        asyncio.run(printer.calibrate_label())
        print("Done.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def run_test_page(model=None):
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        print("Printing test page...")
        asyncio.run(printer.print_test_page())
        print("Done.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def run_sound(action, sound_type, enabled=None, model=None):
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        st = SoundType.BLUETOOTH if sound_type == "bluetooth" else SoundType.POWER
        if action == "get":
            state = asyncio.run(printer.get_sound(st))
            print(f"  {sound_type} sound: {'on' if state else 'off'}")
        else:
            asyncio.run(printer.set_sound(st, enabled))
            print(f"  {sound_type} sound set to {'on' if enabled else 'off'}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def run_shutdown_time(minutes, model=None):
    time_map = {15: 1, 30: 2, 45: 3, 60: 4}
    if minutes not in time_map:
        print(f"Error: invalid time. Choose from: 15, 30, 45, 60", file=sys.stderr)
        sys.exit(1)
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        asyncio.run(printer.set_auto_shutdown_time(time_map[minutes]))
        print(f"Auto-shutdown set to {minutes} minutes.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def run_cancel(model=None):
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        asyncio.run(printer.cancel_print())
        print("Print job cancelled.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def run_reset(model=None):
    confirm = input("This will factory reset the printer. Are you sure? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return
    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        asyncio.run(printer.printer_reset())
        print("Printer has been reset to factory settings.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def main():
    parser = argparse.ArgumentParser(
        prog="niim-tex",
        description="LaTeX-to-NIIMBOT label print pipeline",
    )
    parser.add_argument("--list", action="store_true", help="List all supported label sizes")
    parser.add_argument("--model", type=str, default=None,
                        help="Printer model (e.g. d110). Auto-detects if omitted.")

    sub = parser.add_subparsers(dest="command")

    # new
    new_parser = sub.add_parser("new", help="Generate a LaTeX label template")
    new_parser.add_argument("size", nargs="?", help="Label size (e.g. 15x50). Interactive if omitted.")
    new_parser.add_argument("--name", help="Output filename (default: label_WxH.tex)")

    # cable
    cable_parser = sub.add_parser("cable", help="Assemble and print cable labels from half-flag templates")
    cable_parser.add_argument("--front", required=True, help="Front flag half .tex (or full flag/label)")
    cable_parser.add_argument("--back", help="Back flag half .tex (omit to duplicate front)")
    cable_parser.add_argument("--wrap", help="Cable wrap .tex (optional)")
    cable_parser.add_argument("--density", type=int, default=3, choices=range(1, 6),
                              metavar="N", help="Print density 1-5 (default: 3)")
    cable_parser.add_argument("--quantity", type=int, default=1,
                              metavar="N", help="Number of copies (default: 1)")
    cable_parser.add_argument("--label-type", type=int, default=1, choices=[1, 2, 3, 5],
                              metavar="T", help="Label type (default: 1)")
    cable_parser.add_argument("--preview-only", action="store_true",
                              help="Assemble and save preview without printing")

    # info
    sub.add_parser("info", help="Show printer info (battery, firmware, settings)")

    # rfid
    sub.add_parser("rfid", help="Read label roll RFID tag (remaining labels, type)")

    # heartbeat
    sub.add_parser("heartbeat", help="Check printer status (lid, paper, battery)")

    # print
    print_parser = sub.add_parser("print", help="Compile and print a LaTeX label")
    print_parser.add_argument("file", help="Path to .tex file")
    print_parser.add_argument("--density", type=int, default=3, choices=range(1, 6),
                              metavar="N", help="Print density 1-5 (default: 3)")
    print_parser.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                              metavar="DEG", help="Additional rotation (default: 0)")
    print_parser.add_argument("--quantity", type=int, default=1,
                              metavar="N", help="Number of copies (default: 1)")
    print_parser.add_argument("--label-type", type=int, default=1, choices=[1, 2, 3, 5],
                              metavar="T", help="Label type: 1=gaps, 2=black mark, 3=continuous, 5=transparent (default: 1)")
    print_parser.add_argument("--roll", type=str, default=None, metavar="SIZE",
                              help=f"Loaded roll size (e.g. 12x40). Validates image dimensions before printing.")
    print_parser.add_argument("--fit", action="store_true",
                              help="Resize image to fit the label (uses --roll or .tex geometry for target size)")
    print_parser.add_argument("--no-stretch", action="store_true",
                              help="With --fit: preserve aspect ratio and center instead of stretching")

    # feed
    sub.add_parser("feed", help="Feed paper to recalibrate label positioning")

    # test-page
    sub.add_parser("test-page", help="Print the built-in test page")

    # cancel
    sub.add_parser("cancel", help="Cancel an ongoing print job")

    # sound
    sound_parser = sub.add_parser("sound", help="Get or set printer sounds")
    sound_parser.add_argument("sound_type", choices=["bluetooth", "power"],
                              help="Which sound to configure")
    sound_parser.add_argument("state", nargs="?", choices=["on", "off"],
                              help="Set sound on/off (omit to query current state)")

    # shutdown
    shutdown_parser = sub.add_parser("shutdown", help="Set auto-shutdown timer")
    shutdown_parser.add_argument("minutes", type=int, choices=[15, 30, 45, 60],
                                 help="Auto-shutdown time in minutes")

    # reset
    sub.add_parser("reset", help="Factory reset the printer (requires confirmation)")

    args = parser.parse_args()
    model = args.model

    if args.list:
        list_sizes()
        return

    if args.command == "cable":
        run_cable(args.front, back_path=args.back, wrap_path=args.wrap,
                  density=args.density, quantity=args.quantity,
                  label_type=args.label_type, model=model,
                  preview_only=args.preview_only)
    elif args.command == "info":
        run_info(model)
    elif args.command == "rfid":
        run_rfid(model)
    elif args.command == "heartbeat":
        run_heartbeat(model)
    elif args.command == "new":
        if args.size:
            generate_tex(args.size, args.name)
        else:
            interactive_new(args.name)
    elif args.command == "print":
        run_print(args.file, density=args.density, rotate=args.rotate,
                  quantity=args.quantity, label_type=args.label_type, model=model,
                  roll=args.roll, fit=args.fit, no_stretch=args.no_stretch)
    elif args.command == "feed":
        run_feed(model)
    elif args.command == "test-page":
        run_test_page(model)
    elif args.command == "cancel":
        run_cancel(model)
    elif args.command == "sound":
        if args.state:
            run_sound("set", args.sound_type, args.state == "on", model)
        else:
            run_sound("get", args.sound_type, model=model)
    elif args.command == "shutdown":
        run_shutdown_time(args.minutes, model)
    elif args.command == "reset":
        run_reset(model)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
