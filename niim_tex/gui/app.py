"""Main GUI application window for niim-tex."""

import os
import sys

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QStatusBar, QLabel, QWidget,
    QHBoxLayout,
)
from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtGui import QIcon

from .ble_thread import BleThread
from .device_panel import DevicePanel
from .print_panel import PrintPanel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("niim-tex")
        self.setMinimumSize(600, 400)
        self.resize(900, 650)

        # Load saved geometry
        self.settings = QSettings("niim-tex", "niim-tex-gui")
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

        # BLE thread (persistent connection)
        self.ble = BleThread(self)
        self.ble.start()

        # Status bar
        self._build_status_bar()

        # Tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.device_panel = DevicePanel(self.ble)
        self.print_panel = PrintPanel(self.ble)

        self.tabs.addTab(self.device_panel, "Device")
        self.tabs.addTab(self.print_panel, "Print")

        # Connect status signals
        self.ble.connected.connect(self._on_connected)
        self.ble.disconnected.connect(self._on_disconnected)
        self.ble.status_updated.connect(self._on_status)
        self.ble.print_progress.connect(self._on_print_progress)

        # Load default file if it exists
        default = r"C:\Users\Camden\Documents\Coding Projects\LaTeX to NIIMBOT D110 print\my labels\ski trip photobooth.pdf"
        if os.path.isfile(default):
            self.tabs.setCurrentWidget(self.print_panel)
            self.print_panel.load_files([default])

    def _build_status_bar(self):
        status = QStatusBar()
        self.setStatusBar(status)

        # Connection indicator
        self.status_dot = QLabel("\u2B24")  # filled circle
        self.status_dot.setStyleSheet("color: #e74c3c; font-size: 10px;")
        status.addWidget(self.status_dot)

        self.status_text = QLabel("Disconnected")
        status.addWidget(self.status_text)

        # Spacer
        status.addWidget(QLabel("  "), 1)

        # Battery
        self.battery_label = QLabel("")
        status.addPermanentWidget(self.battery_label)

        # Paper
        self.paper_label = QLabel("")
        status.addPermanentWidget(self.paper_label)

    def _on_connected(self, name):
        self.status_dot.setStyleSheet("color: #27ae60; font-size: 10px;")
        self.status_text.setText(name)

    def _on_disconnected(self):
        self.status_dot.setStyleSheet("color: #e74c3c; font-size: 10px;")
        self.status_text.setText("Disconnected")
        self.battery_label.setText("")
        self.paper_label.setText("")

    def _on_status(self, hb):
        if hb.get("power_level") is not None:
            pct = min(hb["power_level"] * 25, 100)
            self.battery_label.setText(f"Battery: {pct}%")
        if hb.get("paper_state") is not None:
            state = "OK" if hb["paper_state"] == 0 else "Low"
            self.paper_label.setText(f"Paper: {state}")

    def _on_print_progress(self, current, total):
        self.status_text.setText(f"Printing {current}/{total}...")

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.ble.do_disconnect()
        self.ble.stop()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
