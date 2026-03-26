# niim-tex

Design labels in LaTeX with TikZ and print them directly to NIIMBOT thermal printers over BLE.

Currently supports the **D110 / D110-M**. Multi-device support (D11, B21, B1, B18) is planned — see [Roadmap](#roadmap).

## Install

```bash
git clone https://github.com/camdenbalberg/niim-tex.git
cd niim-tex
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate
pip install -r requirements.txt
```

Or install as a package (adds `niim-tex` and `niim-mosaic` to your PATH):

```bash
pip install -e .
```

### Requirements

- **Python 3.10+**
- **pdflatex** (MiKTeX or TeX Live)
- **ImageMagick** (`magick` on PATH)
- **NIIMBOT D110** (tested on D110_M V4 firmware)

## Quick Start

```bash
# Copy a template and edit it
cp templates/15x50.tex my_label.tex
# Add your TikZ content, then print
python niim_tex.py print my_label.tex
```

Or generate a template interactively:

```bash
python niim_tex.py new        # interactive size picker
python niim_tex.py new 12x40  # direct
```

See [examples/example_label.tex](examples/example_label.tex) for a fully designed label.

## Project Structure

```
niim-tex/
├── niim_tex/                  Python package
│   ├── __init__.py            Shared constants (LABEL_SIZES, DPI, mm_to_px)
│   ├── protocol.py            Packet framing, enums
│   ├── printer.py             Base NiimbotPrinter class (BLE, info, settings)
│   ├── models/
│   │   ├── __init__.py        Model registry + get_printer()
│   │   └── d110.py            D110_M V4 print task
│   ├── cli.py                 Main CLI (template generation, compilation, printing)
│   └── mosaic.py              Image-to-label-strip mosaic tool
├── niim_tex.py                Wrapper script (backwards compat)
├── mosaic.py                  Wrapper script (backwards compat)
├── templates/                 Pre-generated LaTeX templates for all label sizes
├── examples/                  Example labels with compiled PDFs
├── builds/                    Compilation output (PDF + PNG, gitignored)
└── pyproject.toml             Package metadata + entry points
```

## Commands

| Command | Description |
|---------|-------------|
| `--list` | Show all supported label sizes with pixel dimensions |
| `--model MODEL` | Specify printer model (e.g. `d110`). Auto-detects if omitted |
| `new [SIZE]` | Generate a LaTeX template (interactive menu if no size given) |
| `print <file.tex>` | Compile LaTeX → PDF → PNG → send to printer |
| `info` | Query printer info (battery, firmware, serial) |
| `rfid` | Read label roll RFID tag (remaining labels, type) |
| `heartbeat` | Check printer status (lid, paper, battery) |
| `feed` | Feed paper to recalibrate label positioning |
| `test-page` | Print the built-in test page |
| `cancel` | Cancel an ongoing print job |
| `sound <bluetooth\|power> [on\|off]` | Get or set printer sounds |
| `shutdown <15\|30\|45\|60>` | Set auto-shutdown timer (minutes) |
| `reset` | Factory reset the printer |

## Print Options

```
python niim_tex.py print label.tex --density 3 --quantity 2 --rotate 0 --label-type 1
```

- `--density N` — Print darkness, 1-5 (default: 3)
- `--quantity N` — Number of copies (default: 1)
- `--rotate DEG` — Additional rotation: 0, 90, 180, 270 (default: 0)
- `--label-type T` — 1=gaps, 2=black mark, 3=continuous, 5=transparent (default: 1)

## Build Output

When you run `print`, compilation artifacts are saved to `builds/<name>/`:

```
builds/
└── my_label/
    ├── my_label.pdf   Compiled PDF
    ├── my_label.png   Rotated bitmap sent to printer
    ├── my_label.aux   LaTeX auxiliary
    └── my_label.log   LaTeX log
```

## Mosaic (Image Tiling)

Split any image into printable label strips. Stick the strips together to recreate the original image in black and white.

```bash
python mosaic.py photo.jpg --preview-only           # preview without printing
python mosaic.py photo.jpg --dry-run                 # show strip count
python mosaic.py photo.jpg                           # print all strips
python mosaic.py photo.jpg --strips 3,5              # reprint specific strips
python mosaic.py photo.jpg --size 15x50 --density 4  # custom size and density
python mosaic.py photo.jpg --no-dither --threshold 128  # hard B&W threshold
```

Output is saved to `mosaic/<image_name>/` with individual strip images and a numbered preview.

## Supported Label Sizes

| Size | Tape Width | Length | Pixels (W x L) |
|------|-----------|--------|-----------------|
| 12x22 | 12mm | 22mm | 96 x 176 |
| 12x30 | 12mm | 30mm | 96 x 240 |
| 12x40 | 12mm | 40mm | 96 x 320 |
| 13x35 | 13mm | 35mm | 104 x 280 |
| 14x25 | 14mm | 25mm | 112 x 200 |
| 14x30 | 14mm | 30mm | 112 x 240 |
| 14x40 | 14mm | 40mm | 112 x 320 |
| 14x60 | 14mm | 60mm | 112 x 480 |
| 15x50 | 15mm | 50mm | 120 x 400 |
| 12.5x74 | 12.5mm | 74mm | 100 x 592 |

> The D110 printhead is 12mm (96px) wide. On tape wider than 12mm, the printable area is still 12mm — templates are automatically capped to this limit.

## Template Anatomy

Templates use landscape orientation (long axis horizontal) for natural text layout:

```latex
\documentclass[10pt]{article}
\usepackage[paperwidth=50mm,paperheight=12mm,margin=0mm]{geometry}
\usepackage{tikz}
\pagestyle{empty}
\topskip=0pt
\parindent=0pt

\begin{document}
\noindent
\begin{tikzpicture}[x=1mm,y=1mm]
  \useasboundingbox (0,0) rectangle (50,12);
  % Canvas: (0,0) to (50,12)
  % Your TikZ content here
\end{tikzpicture}
\end{document}
```

Key details:
- `paperheight` = min(tape_width, 12mm) — capped to the 12mm printhead
- `\useasboundingbox` locks the TikZ canvas to the full page (prevents content-dependent margins)
- `\topskip=0pt` and `\parindent=0pt` eliminate LaTeX's default spacing
- ImageMagick rotates the landscape PDF 90° CW to match the printer's physical feed direction

## Architecture

The codebase is structured as a Python package (`niim_tex/`) with a pluggable model system:

- **`printer.py`** — `NiimbotPrinter` base class with all shared BLE communication, info queries, settings, and calibration commands (~300 lines of shared code)
- **`models/d110.py`** — `D110Printer` subclass implementing the V4 print sequence (~100 lines)
- **`models/__init__.py`** — Model registry with `get_printer(model)` factory function

To add a new printer model, create a subclass of `NiimbotPrinter` in `models/`, implement `print_image()`, and register it in `models/__init__.py`.

## Protocol Notes

The D110_M uses the `D110MV4PrintTask` protocol, which differs significantly from the standard D110:

- **9-byte PrintStart** (not 1-byte) — prevents double-printing
- **No `startPage` command** — skip it entirely
- **13-byte `setPageSize`** — embeds copies count (no separate `setQuantity`)
- **Pixel counts in row headers** — printhead split into 3 chunks
- **All BLE writes use `response=False`** — write-without-response mode
- **One-way heartbeat after `endPrint`** — BLE session cleanup workaround

## Roadmap

Multi-device support is planned. The NIIMBOT printer lineup:

**D-series (12mm / 96px printhead):** D11, D11-H, D110, D110-M, D101
**B-series (48mm / 384px printhead):** B1, B18, B21, B21S, B21 Pro

Each model family uses a different print protocol variant. The goal is a unified Python driver with per-model print task implementations, similar to [niimprint](https://github.com/AndBondStyle/niimprint) but with the LaTeX template pipeline built in.

### Contributing a new model

1. Subclass `NiimbotPrinter` in `niim_tex/models/`
2. Set `MODEL_PREFIXES`, `MAX_WIDTH_PX`, `MAX_DENSITY`
3. Implement `async def print_image(...)`
4. Register in `niim_tex/models/__init__.py`
5. Open a PR with the model name and firmware version you tested on
