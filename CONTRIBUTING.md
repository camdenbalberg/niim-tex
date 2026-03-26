# Contributing to niim-tex

Thanks for your interest in contributing! Whether it's a bug report, new printer model, or feature improvement, contributions are welcome.

## Adding a New Printer Model

This is the most impactful way to contribute. The codebase is designed for this:

1. Subclass `NiimbotPrinter` in `niim_tex/models/`
2. Set `MODEL_PREFIXES`, `MAX_WIDTH_PX`, `MAX_DENSITY`
3. Implement `async def print_image(...)`
4. Register in `niim_tex/models/__init__.py`
5. Open a PR with the model name and firmware version you tested on

See `niim_tex/models/d110.py` for a complete reference implementation.

## Development Setup

```bash
git clone https://github.com/camdenbalberg/niim-tex.git
cd niim-tex
python -m venv .venv
.venv/Scripts/activate    # Windows
# source .venv/bin/activate  # macOS/Linux
pip install -e .
```

You'll also need `pdflatex` and `magick` (ImageMagick) on your PATH.

## Reporting Bugs

Open an issue with:
- Your printer model and firmware version (run `niim-tex info`)
- OS and Python version
- What you expected vs. what happened
- Any error output

## Pull Requests

- Keep PRs focused on a single change
- Test on real hardware if touching printer/protocol code
- Update the README if adding new commands or changing behavior

## Code Style

- Follow existing patterns in the codebase
- Use type hints for function signatures
- Keep modules focused: protocol logic in `protocol.py`, BLE communication in `printer.py`, model-specific sequences in `models/`
