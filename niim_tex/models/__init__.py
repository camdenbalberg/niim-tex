"""NIIMBOT model registry — maps model names to printer drivers."""

from .b1 import B1Printer
from .d110 import D110Printer

MODELS = {
    "d110": D110Printer,
    "b1":   B1Printer,
}

# Aliases for common variant names
ALIASES = {
    "b1pro": "b1",
    "d110m": "d110",
}

# All known BLE name prefixes across supported models
ALL_PREFIXES = []
for cls in MODELS.values():
    ALL_PREFIXES.extend(cls.MODEL_PREFIXES)


def get_printer(model=None, prefer_usb=True):
    """Return a printer instance.

    Args:
        model: Model name (e.g. "d110", "b1"). If None, auto-detects any known printer.
        prefer_usb: If True, check for USB serial connection before BLE.
    """
    if model:
        key = model.lower().replace("-", "").replace("_", "").replace(" ", "")
        key = ALIASES.get(key, key)
        cls = MODELS.get(key)
        if not cls:
            available = ", ".join(MODELS.keys())
            raise ValueError(f"Unknown model: {model}. Available: {available}")
        printer = cls()
        # Try USB first if available
        if prefer_usb and hasattr(printer, 'connect_usb'):
            try:
                from ..transport import find_niimbot_usb
                port = find_niimbot_usb()
                if port:
                    printer.connect_usb(port)
                    print(f"Connected via USB: {port}")
            except Exception:
                pass  # Fall back to BLE
        return printer

    # Auto-detect: return a printer that scans for ALL known prefixes
    return _AutoDetectPrinter()


class _AutoDetectPrinter:
    """Proxy that scans for any known printer and delegates to the matched model."""

    def __getattr__(self, name):
        if name == "is_connected":
            return False
        raise RuntimeError(
            "Auto-detect printer must be connected first. Call connect()."
        )

    async def disconnect(self):
        """No-op before connection."""
        pass

    async def connect(self):
        """Scan for any known printer, identify the model, and connect."""
        import asyncio
        from bleak import BleakScanner

        found_device = None
        found_cls = None
        model_classes = list(MODELS.values())

        def on_detect(device, _adv):
            nonlocal found_device, found_cls
            if found_device:
                return
            if not device.name:
                return
            for cls in model_classes:
                if cls._matches_name(device.name):
                    found_device = device
                    found_cls = cls
                    return

        scanner = BleakScanner(detection_callback=on_detect)
        await scanner.start()
        for _ in range(100):  # 10 seconds
            if found_device:
                break
            await asyncio.sleep(0.1)
        await scanner.stop()

        if not found_device:
            names = ", ".join(ALL_PREFIXES)
            raise RuntimeError(
                f"No printer found (scanned for: {names}). "
                "Make sure it's powered on and nearby."
            )

        # Create the real printer and connect to the found device
        printer = found_cls()
        from bleak import BleakClient
        printer.client = BleakClient(found_device.address)
        await printer.client.connect()
        printer.char_uuid = await printer._find_char(printer.client)
        printer._device_name = found_device.name

        # Replace ourselves with the real printer in the caller's scope
        self.__class__ = printer.__class__
        self.__dict__ = printer.__dict__
        return found_device.name
