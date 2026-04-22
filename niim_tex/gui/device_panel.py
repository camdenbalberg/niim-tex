"""Device Details panel — connection, info, settings, calibration."""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QSlider, QCheckBox, QSpinBox, QFormLayout, QProgressBar,
    QMessageBox,
)
from PyQt6.QtCore import Qt

from niim_tex.protocol import SoundType
from niim_tex.models import MODELS


class DevicePanel(QWidget):
    def __init__(self, ble_thread, parent=None):
        super().__init__(parent)
        self.ble = ble_thread
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── Connection ───────────────────────────────────────────────
        conn_group = QGroupBox("Connection")
        conn_layout = QHBoxLayout(conn_group)

        self.model_combo = QComboBox()
        self.model_combo.addItem("Auto-detect", None)
        for key in MODELS:
            self.model_combo.addItem(key.upper(), key)
        conn_layout.addWidget(QLabel("Model:"))
        conn_layout.addWidget(self.model_combo)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFixedWidth(120)
        conn_layout.addWidget(self.connect_btn)

        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
        conn_layout.addWidget(self.status_label)
        conn_layout.addStretch()

        layout.addWidget(conn_group)

        # ── Device Info ──────────────────────────────────────────────
        info_group = QGroupBox("Device Info")
        info_layout = QFormLayout(info_group)

        self.device_name_label = QLabel("—")
        self.serial_label = QLabel("—")
        self.firmware_label = QLabel("—")
        self.hardware_label = QLabel("—")
        self.bt_addr_label = QLabel("—")
        self.device_type_label = QLabel("—")

        info_layout.addRow("Device Name:", self.device_name_label)
        info_layout.addRow("Serial:", self.serial_label)
        info_layout.addRow("Firmware:", self.firmware_label)
        info_layout.addRow("Hardware:", self.hardware_label)
        info_layout.addRow("BT Address:", self.bt_addr_label)
        info_layout.addRow("Device Type:", self.device_type_label)

        layout.addWidget(info_group)

        # ── Battery & Paper ──────────────────────────────────────────
        status_group = QGroupBox("Battery & Paper")
        status_layout = QFormLayout(status_group)

        self.battery_bar = QProgressBar()
        self.battery_bar.setMaximum(100)
        self.battery_bar.setFormat("%v%")
        status_layout.addRow("Battery:", self.battery_bar)

        self.paper_label = QLabel("—")
        self.roll_label = QLabel("—")
        status_layout.addRow("Remaining:", self.paper_label)
        status_layout.addRow("Roll:", self.roll_label)

        layout.addWidget(status_group)

        # ── Settings ─────────────────────────────────────────────────
        settings_group = QGroupBox("Settings")
        settings_layout = QFormLayout(settings_group)

        self.shutdown_combo = QComboBox()
        for label, val in [("15 min", 1), ("30 min", 2), ("45 min", 3), ("60 min", 4)]:
            self.shutdown_combo.addItem(label, val)
        self.shutdown_combo.currentIndexChanged.connect(self._on_shutdown_changed)
        settings_layout.addRow("Auto Power-off:", self.shutdown_combo)

        self.power_sound_cb = QCheckBox("Enabled")
        self.power_sound_cb.stateChanged.connect(
            lambda s: self.ble.do_set_sound(SoundType.POWER, s == Qt.CheckState.Checked.value))
        settings_layout.addRow("Power Sound:", self.power_sound_cb)

        self.bt_sound_cb = QCheckBox("Enabled")
        self.bt_sound_cb.stateChanged.connect(
            lambda s: self.ble.do_set_sound(SoundType.BLUETOOTH, s == Qt.CheckState.Checked.value))
        settings_layout.addRow("BT Sound:", self.bt_sound_cb)

        layout.addWidget(settings_group)

        # ── Calibration ──────────────────────────────────────────────
        cal_group = QGroupBox("Calibration & Offset")
        cal_layout = QFormLayout(cal_group)

        self.hoffset_spin = QSpinBox()
        self.hoffset_spin.setRange(-20, 20)
        self.hoffset_spin.setSuffix(" px")
        cal_layout.addRow("H Offset:", self.hoffset_spin)

        self.voffset_spin = QSpinBox()
        self.voffset_spin.setRange(-20, 20)
        self.voffset_spin.setSuffix(" px")
        cal_layout.addRow("V Offset:", self.voffset_spin)

        cal_btn_layout = QHBoxLayout()
        self.feed_btn = QPushButton("Feed / Calibrate")
        self.feed_btn.clicked.connect(self.ble.do_calibrate)
        cal_btn_layout.addWidget(self.feed_btn)

        self.test_btn = QPushButton("Test Page")
        self.test_btn.clicked.connect(self.ble.do_test_page)
        cal_btn_layout.addWidget(self.test_btn)
        cal_layout.addRow(cal_btn_layout)

        layout.addWidget(cal_group)

        # ── Bottom buttons ───────────────────────────────────────────
        btn_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh All")
        self.refresh_btn.clicked.connect(self._refresh_all)
        btn_layout.addWidget(self.refresh_btn)

        self.reset_btn = QPushButton("Factory Reset")
        self.reset_btn.setStyleSheet("color: #e74c3c;")
        self.reset_btn.clicked.connect(self._on_factory_reset)
        btn_layout.addWidget(self.reset_btn)
        btn_layout.addStretch()
        layout.addWidget(QWidget())  # spacer
        layout.addLayout(btn_layout)

        layout.addStretch()

        # Disable settings until connected
        self._set_controls_enabled(False)

    def _connect_signals(self):
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        self.ble.connected.connect(self._on_connected)
        self.ble.disconnected.connect(self._on_disconnected)
        self.ble.result.connect(self._on_result)
        self.ble.error.connect(self._on_error)
        self.ble.status_updated.connect(self._on_status)

    # ── Actions ──────────────────────────────────────────────────────

    def _on_connect_clicked(self):
        if self.ble.is_connected:
            self.ble.do_disconnect()
        else:
            model = self.model_combo.currentData()
            self.connect_btn.setEnabled(False)
            self.connect_btn.setText("Connecting...")
            self.status_label.setText("Scanning...")
            self.status_label.setStyleSheet("color: #f39c12; font-weight: bold;")
            self.ble.do_connect(model)

    def _on_connected(self, name):
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("Disconnect")
        self.status_label.setText(f"Connected: {name}")
        self.status_label.setStyleSheet("color: #27ae60; font-weight: bold;")
        self.device_name_label.setText(name)
        self._set_controls_enabled(True)
        self._refresh_all()

    def _on_disconnected(self):
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("Connect")
        self.status_label.setText("Disconnected")
        self.status_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
        self._set_controls_enabled(False)
        self._clear_info()

    def _refresh_all(self):
        self.ble.do_get_info()
        self.ble.do_get_rfid()
        self.ble.do_heartbeat()
        self.ble.do_get_sound(SoundType.POWER)
        self.ble.do_get_sound(SoundType.BLUETOOTH)

    def _on_shutdown_changed(self, idx):
        val = self.shutdown_combo.currentData()
        if val and self.ble.is_connected:
            self.ble.do_set_shutdown(val)

    def _on_factory_reset(self):
        reply = QMessageBox.warning(
            self, "Factory Reset",
            "This will reset the printer to factory settings. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.ble.submit("reset", self.ble.printer.printer_reset)

    # ── Result handling ──────────────────────────────────────────────

    def _on_result(self, op_id, data):
        if op_id == "info" and isinstance(data, dict):
            self.serial_label.setText(str(data.get("serial", "—")))
            self.firmware_label.setText(str(data.get("software", "—")))
            self.hardware_label.setText(str(data.get("hardware", "—")))
            self.bt_addr_label.setText(str(data.get("bluetooth_address", "—")))
            self.device_type_label.setText(str(data.get("device_type", "—")))
            self.battery_bar.setValue(data.get("battery", 0))

            # Set shutdown combo without triggering signal
            shutdown = data.get("auto_shutdown_time", 0)
            idx = self.shutdown_combo.findData(shutdown)
            if idx >= 0:
                self.shutdown_combo.blockSignals(True)
                self.shutdown_combo.setCurrentIndex(idx)
                self.shutdown_combo.blockSignals(False)

        elif op_id == "rfid" and isinstance(data, dict):
            remaining = data.get("_remaining", "?")
            total = data.get("_total", "?")
            self.paper_label.setText(f"{remaining} / {total} labels")
            lookup = data.get("_lookup")
            if lookup:
                self.roll_label.setText(f"{lookup['model']} ({lookup['size_key']})")
            else:
                self.roll_label.setText(f"Barcode: {data.get('barcode', '?')}")

        elif op_id == "get_sound":
            # data is True/False — but we need to know which sound
            pass  # handled via separate callbacks if needed

    def _on_error(self, op_id, msg):
        if op_id == "connect":
            self.connect_btn.setEnabled(True)
            self.connect_btn.setText("Connect")
            self.status_label.setText("Connection failed")
            self.status_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
            QMessageBox.critical(self, "Connection Error", msg)

    def _on_status(self, hb):
        if hb.get("power_level") is not None:
            self.battery_bar.setValue(min(hb["power_level"] * 25, 100))

    # ── Helpers ──────────────────────────────────────────────────────

    def _set_controls_enabled(self, enabled):
        for w in [self.shutdown_combo, self.power_sound_cb, self.bt_sound_cb,
                  self.hoffset_spin, self.voffset_spin, self.feed_btn,
                  self.test_btn, self.refresh_btn, self.reset_btn]:
            w.setEnabled(enabled)

    def _clear_info(self):
        for lbl in [self.device_name_label, self.serial_label, self.firmware_label,
                    self.hardware_label, self.bt_addr_label, self.device_type_label,
                    self.paper_label, self.roll_label]:
            lbl.setText("—")
        self.battery_bar.setValue(0)
