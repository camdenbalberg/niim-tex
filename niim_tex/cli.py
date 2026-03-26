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

from niim_tex import DPI, MM_PER_INCH, LABEL_SIZES, mm_to_px
from niim_tex.protocol import LabelType, SoundType
from niim_tex.models import get_printer


def list_sizes():
    print(f"{'Size':>10}  {'Width':>6}  {'Length':>7}  {'W (px)':>6}  {'L (px)':>7}  {'Note'}")
    print(f"{'':->10}  {'':->6}  {'':->7}  {'':->6}  {'':->7}  {'':->20}")
    for name, (w, l) in LABEL_SIZES.items():
        note = "cable label" if "." in name else ""
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
    if "." in size_key:
        cable_comment = (
            "% NOTE: This is a cable label (T12.5*74+35).\n"
            "%   - The 74mm flag folds in half at the 37mm midpoint.\n"
            "%   - Both halves stick together (non-sticky flag).\n"
            "%   - The 35mm cable wrap portion is beyond this template.\n"
            "%   - Consider designing for two mirrored halves,\n"
            "%     or a single design centered on one half (37mm).\n"
        )

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
        extra = " (cable)" if "." in name else ""
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


def run_print(tex_path, density=3, rotate=0, quantity=1, label_type=1, model=None):
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

    # Structured build output directory
    build_dir = os.path.join(os.getcwd(), "builds", base_name)
    os.makedirs(build_dir, exist_ok=True)
    pdf_path = os.path.join(build_dir, base_name + ".pdf")
    png_path = os.path.join(build_dir, base_name + ".png")

    # Step 1: pdflatex
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

    # Step 2: PDF -> PNG via ImageMagick
    # Landscape PDF gets rotated 90 CW to portrait for the printer
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

    # Step 3: Sanity check dimensions
    if tape_w and label_l:
        expected_w = mm_to_px(tape_w)
        expected_h = mm_to_px(label_l)
        id_result = subprocess.run(
            ["magick", "identify", "-format", "%w %h", png_path],
            capture_output=True, text=True,
        )
        if id_result.returncode == 0:
            parts = id_result.stdout.strip().split()
            if len(parts) == 2:
                actual_w, actual_h = int(parts[0]), int(parts[1])
                tolerance = 5  # pixels
                if abs(actual_w - expected_w) > tolerance or abs(actual_h - expected_h) > tolerance:
                    print(f"Warning: image is {actual_w}x{actual_h}px, "
                          f"expected ~{expected_w}x{expected_h}px for {size_name or f'{tape_w}x{label_l}mm'}")

    print(f"Build output saved to {build_dir}/")

    # Step 4: Send to printer
    print("Sending to printer...")
    img = Image.open(png_path)

    if rotate != 0:
        img = img.rotate(-rotate, expand=True)

    printer = get_printer(model)
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
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

    if args.command == "info":
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
                  quantity=args.quantity, label_type=args.label_type, model=model)
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
