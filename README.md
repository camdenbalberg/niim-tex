# niim-tex

LaTeX-to-NIIMBOT D110 label print pipeline. Design labels in LaTeX with TikZ, compile to PDF, and print directly to a D110 thermal printer over BLE — all in one command.

## Requirements

- **Python 3.10+** with packages: `bleak`, `Pillow`
- **pdflatex** (MiKTeX or TeX Live)
- **ImageMagick** (`magick` on PATH)
- **NIIMBOT D110** (tested on D110_M V4 firmware)

```
pip install bleak Pillow
```

## Quick Start

```bash
# 1. Generate a label template
python niim_tex.py new 15x50

# 2. Edit the .tex file — add your content with TikZ
# 3. Print it
python niim_tex.py print label_15x50.tex
```

## Commands

| Command | Description |
|---------|-------------|
| `--list` | Show all supported label sizes with pixel dimensions |
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

- `--density N` — Print darkness, 1-3 (default: 3)
- `--quantity N` — Number of copies (default: 1)
- `--rotate DEG` — Additional rotation: 0, 90, 180, 270 (default: 0)
- `--label-type T` — 1=gaps, 2=black mark, 3=continuous, 5=transparent (default: 1)

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

> The D110 printhead is 12mm (96px) wide. On tape wider than 12mm, templates are automatically capped to the 12mm printable area.

## How It Works

1. **Template generation** (`new`) — Creates a `.tex` file with correct `geometry` dimensions, TikZ canvas, and `\useasboundingbox` for pixel-perfect output
2. **Compilation** (`print`) — Runs `pdflatex` → `magick` (203 DPI, rotate 90° CW, grayscale) → sends bitmap rows over BLE
3. **BLE driver** (`d110.py`) — Custom async driver implementing the D110_M V4 protocol (9-byte PrintStart, 13-byte page size, pixel-counted row headers, write-without-response)

## Template Anatomy

Generated templates use landscape orientation (long axis horizontal) for natural text layout:

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

## Files

| File | Description |
|------|-------------|
| `niim_tex.py` | CLI tool — template generation, compilation, printing |
| `d110.py` | Async BLE driver for D110_M V4 protocol |
| `test_print.py` | Debug script used during protocol reverse engineering |

## Protocol Notes

The D110_M uses the `D110MV4PrintTask` protocol, which differs significantly from the standard D110:

- **9-byte PrintStart** (not 1-byte) — prevents double-printing
- **No `startPage` command** — skip it entirely
- **13-byte `setPageSize`** — embeds copies count (no separate `setQuantity`)
- **Pixel counts in row headers** — printhead split into 3 chunks
- **All BLE writes use `response=False`** — write-without-response mode
- **One-way heartbeat after `endPrint`** — BLE session cleanup workaround
