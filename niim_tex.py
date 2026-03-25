#!/usr/bin/env python3
"""niim-tex: LaTeX-to-NIIMBOT D110 label print pipeline."""

import argparse
import asyncio
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

from d110 import D110

DPI = 203  # D110 native resolution
MM_PER_INCH = 25.4

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
    return round(mm * DPI / MM_PER_INCH)


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
    margin = 1  # mm

    # Landscape: long axis = paperwidth, short axis = paperheight
    pw = label_l
    ph = tape_w
    usable_w = pw - 2 * margin
    usable_h = ph - 2 * margin

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
        f"\\usepackage[paperwidth={pw}mm,paperheight={ph}mm,margin={margin}mm]{{geometry}}",
        f"\\pagestyle{{empty}}",
        f"",
        f"\\begin{{document}}",
        f"% Label: {size_key} ({tape_w}mm tape width x {label_l}mm length)",
        f"% Usable area: {usable_w}mm x {usable_h}mm (with {margin}mm margins)",
        f"% Print with: python niim_tex.py print {filename}",
        f"%",
    ]
    if cable_comment:
        lines.append(cable_comment.rstrip())
    lines.extend([
        f"% --- Your content below ---",
        f"",
        f"Hello",
        f"",
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


def run_print(tex_path, density=3, rotate=0, quantity=1):
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
    tex_dir = os.path.dirname(tex_path)
    base_name = os.path.splitext(os.path.basename(tex_path))[0]

    # Parse geometry for sanity checking
    pw, ph = parse_geometry_from_tex(tex_path)
    size_name, tape_w, label_l = None, None, None
    if pw and ph:
        size_name, tape_w, label_l = find_label_size_for_geometry(pw, ph)

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, base_name + ".pdf")
        png_path = os.path.join(tmpdir, base_name + ".png")

        # Step 1: pdflatex (output everything to temp dir)
        print(f"Compiling {os.path.basename(tex_path)}...")
        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
             f"-output-directory={tmpdir}", tex_path],
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
        # Landscape PDF gets rotated 90° CW to portrait for the printer
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

        # Copy PDF to source directory (the only artifact the user wants)
        final_pdf = os.path.join(tex_dir, base_name + ".pdf")
        shutil.copy2(pdf_path, final_pdf)
        print(f"PDF saved to {final_pdf}")

        # Step 4: Send to printer
        print("Sending to D110 printer...")
        img = Image.open(png_path)
        if rotate != 0:
            img = img.rotate(-rotate, expand=True)

        printer = D110()
        try:
            name = asyncio.run(printer.connect())
            print(f"Connected to {name}")
            asyncio.run(printer.print_image(img, density=density, quantity=quantity))
            print("Print job completed.")
        except Exception as e:
            print(f"Print failed: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            asyncio.run(printer.disconnect())

    # temp dir auto-cleaned here — only the PDF remains
    print("Done.")


def run_info():
    printer = D110()
    try:
        name = asyncio.run(printer.connect())
        print(f"Connected to {name}")
        info = asyncio.run(printer.get_info())
        print(f"  Serial:   {info['serial']}")
        print(f"  Software: {info['software']}")
        print(f"  Hardware: {info['hardware']}")
        print(f"  Battery:  {info['battery']}%")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(printer.disconnect())


def main():
    parser = argparse.ArgumentParser(
        prog="niim-tex",
        description="LaTeX-to-NIIMBOT D110 label print pipeline",
    )
    parser.add_argument("--list", action="store_true", help="List all supported label sizes")

    sub = parser.add_subparsers(dest="command")

    # new
    new_parser = sub.add_parser("new", help="Generate a LaTeX label template")
    new_parser.add_argument("size", nargs="?", help="Label size (e.g. 15x50). Interactive if omitted.")
    new_parser.add_argument("--name", help="Output filename (default: label_WxH.tex)")

    # info
    sub.add_parser("info", help="Show D110 printer info (battery, firmware)")

    # print
    print_parser = sub.add_parser("print", help="Compile and print a LaTeX label")
    print_parser.add_argument("file", help="Path to .tex file")
    print_parser.add_argument("--density", type=int, default=3, choices=range(1, 6),
                              metavar="N", help="Print density 1-5 (default: 3)")
    print_parser.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                              metavar="DEG", help="Additional rotation (default: 0)")
    print_parser.add_argument("--quantity", type=int, default=1,
                              metavar="N", help="Number of copies (default: 1)")

    args = parser.parse_args()

    if args.list:
        list_sizes()
        return

    if args.command == "info":
        run_info()
    elif args.command == "new":
        if args.size:
            generate_tex(args.size, args.name)
        else:
            interactive_new(args.name)
    elif args.command == "print":
        run_print(args.file, density=args.density, rotate=args.rotate, quantity=args.quantity)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
