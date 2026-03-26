"""NIIMBOT model registry — maps model names to printer drivers."""

from .d110 import D110Printer

MODELS = {
    "d110": D110Printer,
}

# All known BLE name prefixes across supported models
ALL_PREFIXES = []
for cls in MODELS.values():
    ALL_PREFIXES.extend(cls.MODEL_PREFIXES)


def get_printer(model=None):
    """Return a printer instance.

    Args:
        model: Model name (e.g. "d110"). If None, auto-detects any known printer.
    """
    if model:
        key = model.lower().replace("-", "").replace("_", "")
        cls = MODELS.get(key)
        if not cls:
            available = ", ".join(MODELS.keys())
            raise ValueError(f"Unknown model: {model}. Available: {available}")
        return cls()

    # Auto-detect: try D110 first (only tested model), expand as models are added
    printer = D110Printer()
    return printer
