"""niim-tex GUI — PyQt6 interface for NIIMBOT thermal label printers."""


def launch():
    """Entry point for the GUI application."""
    from .app import main
    main()
