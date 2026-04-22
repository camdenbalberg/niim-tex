"""Print panel — file picker, live B&W preview, settings, and print button."""

import os
import math

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QSlider, QDoubleSpinBox, QSpinBox, QCheckBox, QFileDialog,
    QListWidget, QListWidgetItem, QRadioButton, QButtonGroup, QMessageBox,
    QScrollArea, QSplitter, QApplication,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QImage, QPixmap

from PIL import Image

from niim_tex import DPI, LABEL_SIZES, mm_to_px
from niim_tex.cli import open_image, _prepare_label_image, compile_tex_to_png


def pil_to_qpixmap(pil_image):
    """Convert a PIL Image to a QPixmap for display."""
    if pil_image.mode == "1":
        pil_image = pil_image.convert("L")
    elif pil_image.mode not in ("L", "RGB", "RGBA"):
        pil_image = pil_image.convert("RGB")

    if pil_image.mode == "L":
        data = pil_image.tobytes("raw", "L")
        qimg = QImage(data, pil_image.width, pil_image.height,
                      pil_image.width, QImage.Format.Format_Grayscale8)
    elif pil_image.mode == "RGB":
        data = pil_image.tobytes("raw", "RGB")
        qimg = QImage(data, pil_image.width, pil_image.height,
                      pil_image.width * 3, QImage.Format.Format_RGB888)
    else:
        pil_image = pil_image.convert("RGB")
        data = pil_image.tobytes("raw", "RGB")
        qimg = QImage(data, pil_image.width, pil_image.height,
                      pil_image.width * 3, QImage.Format.Format_RGB888)

    return QPixmap.fromImage(qimg)


class PreviewWorker(QThread):
    """Background thread for image processing (keeps UI responsive)."""
    finished = pyqtSignal(object, str)  # (QPixmap, info_text)

    def __init__(self, image, target_w, target_h, dither, threshold,
                 no_stretch, crop, align, gamma, parent=None):
        super().__init__(parent)
        self.image = image
        self.target_w = target_w
        self.target_h = target_h
        self.dither = dither
        self.threshold = threshold
        self.no_stretch = no_stretch
        self.crop = crop
        self.align = align
        self.gamma = gamma

    def run(self):
        try:
            label = _prepare_label_image(
                self.image, self.target_w, self.target_h,
                self.dither, self.threshold, 0,
                self.no_stretch, self.align, self.gamma, self.crop)
            pixmap = pil_to_qpixmap(label)
            info = f"{label.width}x{label.height}px"
            self.finished.emit(pixmap, info)
        except Exception as e:
            self.finished.emit(None, f"Error: {e}")


class PrintPanel(QWidget):
    def __init__(self, ble_thread, parent=None):
        super().__init__(parent)
        self.ble = ble_thread
        self._images = []       # [(filename, PIL.Image), ...]
        self._current_idx = 0
        self._preview_worker = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._generate_preview)
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout = QHBoxLayout(self)
        layout.addWidget(splitter)

        # ── Left: Files + Settings (scrollable) ──────────────────────
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        left_scroll.setMinimumWidth(320)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_scroll.setWidget(left)

        # File picker
        file_group = QGroupBox("Files")
        file_layout = QVBoxLayout(file_group)
        btn_row = QHBoxLayout()
        self.add_files_btn = QPushButton("Add Files...")
        self.add_files_btn.clicked.connect(self._on_add_files)
        btn_row.addWidget(self.add_files_btn)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        btn_row.addWidget(self.clear_btn)
        file_layout.addLayout(btn_row)
        self.file_list = QListWidget()
        self.file_list.currentRowChanged.connect(self._on_file_selected)
        file_layout.addWidget(self.file_list)
        left_layout.addWidget(file_group)

        # Label settings
        settings_group = QGroupBox("Label Settings")
        settings_layout = QVBoxLayout(settings_group)

        # Roll size
        roll_row = QHBoxLayout()
        roll_row.addWidget(QLabel("Roll:"))
        self.roll_combo = QComboBox()
        self.roll_combo.addItem("Auto (RFID)", None)
        # Group by series
        for name, (tw, ll) in LABEL_SIZES.items():
            self.roll_combo.addItem(f"{name}  ({tw}x{ll}mm)", name)
        self.roll_combo.currentIndexChanged.connect(self._schedule_preview)
        roll_row.addWidget(self.roll_combo, 1)
        settings_layout.addLayout(roll_row)

        # Density
        dens_row = QHBoxLayout()
        dens_row.addWidget(QLabel("Density:"))
        self.density_spin = QSpinBox()
        self.density_spin.setRange(1, 5)
        self.density_spin.setValue(3)
        dens_row.addWidget(self.density_spin)
        dens_row.addStretch()
        settings_layout.addLayout(dens_row)

        # Gamma
        gamma_row = QHBoxLayout()
        gamma_row.addWidget(QLabel("Gamma:"))
        self.gamma_spin = QDoubleSpinBox()
        self.gamma_spin.setRange(0.1, 3.0)
        self.gamma_spin.setSingleStep(0.05)
        self.gamma_spin.setValue(0.55)
        self.gamma_spin.valueChanged.connect(self._schedule_preview)
        gamma_row.addWidget(self.gamma_spin)
        gamma_row.addStretch()
        settings_layout.addLayout(gamma_row)

        # Resize mode
        mode_row = QHBoxLayout()
        self.stretch_radio = QRadioButton("Stretch")
        self.no_stretch_radio = QRadioButton("Fit (letterbox)")
        self.crop_radio = QRadioButton("Crop (fill)")
        self.stretch_radio.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.stretch_radio, 0)
        self.mode_group.addButton(self.no_stretch_radio, 1)
        self.mode_group.addButton(self.crop_radio, 2)
        self.mode_group.idToggled.connect(lambda *_: self._schedule_preview())
        mode_row.addWidget(self.stretch_radio)
        mode_row.addWidget(self.no_stretch_radio)
        mode_row.addWidget(self.crop_radio)
        settings_layout.addLayout(mode_row)

        # Align
        align_row = QHBoxLayout()
        align_row.addWidget(QLabel("Align:"))
        self.align_combo = QComboBox()
        self.align_combo.addItems(["center", "start", "end"])
        self.align_combo.currentIndexChanged.connect(self._schedule_preview)
        align_row.addWidget(self.align_combo)
        align_row.addStretch()
        settings_layout.addLayout(align_row)

        # Dither
        dither_row = QHBoxLayout()
        self.dither_cb = QCheckBox("Dither")
        self.dither_cb.setChecked(True)
        self.dither_cb.stateChanged.connect(self._schedule_preview)
        dither_row.addWidget(self.dither_cb)
        dither_row.addWidget(QLabel("Threshold:"))
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(0, 255)
        self.threshold_spin.setValue(128)
        self.threshold_spin.valueChanged.connect(self._schedule_preview)
        dither_row.addWidget(self.threshold_spin)
        settings_layout.addLayout(dither_row)

        # Quantity
        qty_row = QHBoxLayout()
        qty_row.addWidget(QLabel("Copies:"))
        self.quantity_spin = QSpinBox()
        self.quantity_spin.setRange(1, 99)
        self.quantity_spin.setValue(1)
        qty_row.addWidget(self.quantity_spin)
        qty_row.addStretch()
        settings_layout.addLayout(qty_row)

        left_layout.addWidget(settings_group)

        # Print button
        self.print_btn = QPushButton("Print")
        self.print_btn.setMinimumHeight(40)
        self.print_btn.setStyleSheet(
            "QPushButton { background-color: #27ae60; color: white; "
            "font-size: 14px; font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background-color: #2ecc71; }"
            "QPushButton:disabled { background-color: #95a5a6; }"
        )
        self.print_btn.clicked.connect(self._on_print)
        left_layout.addWidget(self.print_btn)

        splitter.addWidget(left_scroll)

        # ── Right: Preview ───────────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("Print Preview"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label = QLabel("Select an image to preview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background-color: #ecf0f1; border: 1px solid #bdc3c7;")
        scroll.setWidget(self.preview_label)
        right_layout.addWidget(scroll, 1)

        self.info_label = QLabel("")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_label.setStyleSheet("color: #7f8c8d;")
        right_layout.addWidget(self.info_label)

        splitter.addWidget(right)
        splitter.setSizes([350, 450])

    def _connect_signals(self):
        self.ble.connected.connect(self._on_printer_connected)
        self.ble.disconnected.connect(self._on_printer_disconnected)
        self.ble.result.connect(self._on_result)
        self.ble.error.connect(self._on_error)

    # ── File management ──────────────────────────────────────────────

    def _on_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Images",
            "",
            "Images & PDFs (*.png *.jpg *.jpeg *.bmp *.gif *.tiff *.tif "
            "*.webp *.avif *.heif *.heic *.pdf *.tex);;"
            "All Files (*)",
        )
        if not paths:
            return

        printer_dpi = getattr(self.ble.printer, 'DPI', DPI) if self.ble.printer else 300
        for p in paths:
            try:
                if p.lower().endswith('.tex'):
                    # Compile .tex to PNG
                    rotate = 90  # default
                    printer_printable = getattr(self.ble.printer, 'PRINTABLE_HEIGHT_MM', 12) if self.ble.printer else 50
                    from niim_tex.cli import parse_geometry_from_tex, find_label_size_for_geometry
                    pw, ph = parse_geometry_from_tex(p)
                    if pw and ph and pw < ph:
                        rotate = 0
                    elif pw and ph:
                        _, _, ll = find_label_size_for_geometry(pw, ph)
                        if ll and printer_printable >= ll:
                            rotate = 0
                    png_path, _ = compile_tex_to_png(p, dpi=printer_dpi, rotate=rotate)
                    img = Image.open(png_path).convert("L")
                else:
                    img = open_image(p, dpi=printer_dpi).convert("L")
                name = os.path.basename(p)
                self._images.append((name, img))
                self.file_list.addItem(name)
            except Exception as e:
                QMessageBox.warning(self, "Open Error", f"Could not open {os.path.basename(p)}:\n{e}")

        if self.file_list.count() > 0 and self.file_list.currentRow() < 0:
            self.file_list.setCurrentRow(0)

    def _on_clear(self):
        self._images.clear()
        self.file_list.clear()
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText("Select an image to preview")
        self.info_label.setText("")

    def _on_file_selected(self, row):
        self._current_idx = row
        self._schedule_preview()

    # ── Preview generation ───────────────────────────────────────────

    def _schedule_preview(self):
        self._preview_timer.start()

    def _get_target_dims(self):
        roll = self.roll_combo.currentData()
        if not roll or roll not in LABEL_SIZES:
            # Try to get from printer RFID
            return None, None
        tape_w, label_l = LABEL_SIZES[roll]
        printer_dpi = getattr(self.ble.printer, 'DPI', DPI) if self.ble.printer else 300
        printable_mm = getattr(self.ble.printer, 'PRINTABLE_HEIGHT_MM', 50) if self.ble.printer else 50
        actual_printable = min(tape_w, printable_mm)
        return mm_to_px(actual_printable, printer_dpi), mm_to_px(label_l, printer_dpi)

    def _generate_preview(self):
        if self._current_idx < 0 or self._current_idx >= len(self._images):
            return
        target_w, target_h = self._get_target_dims()
        if not target_w:
            self.info_label.setText("Select a roll size for preview")
            return

        _, img = self._images[self._current_idx]
        mode_id = self.mode_group.checkedId()
        no_stretch = mode_id == 1
        crop = mode_id == 2
        align = self.align_combo.currentText()
        gamma = self.gamma_spin.value()
        dither = self.dither_cb.isChecked()
        threshold = self.threshold_spin.value()

        self._preview_worker = PreviewWorker(
            img, target_w, target_h, dither, threshold,
            no_stretch, crop, align, gamma, self)
        self._preview_worker.finished.connect(self._on_preview_done)
        self._preview_worker.start()

    def _on_preview_done(self, pixmap, info):
        if pixmap:
            # Scale to fit the preview area
            scaled = pixmap.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self.preview_label.setPixmap(scaled)
            self.preview_label.setText("")
        self.info_label.setText(info)

    # ── Printing ─────────────────────────────────────────────────────

    def _on_print(self):
        if not self._images:
            QMessageBox.warning(self, "No Images", "Add images to print first.")
            return
        if not self.ble.is_connected:
            QMessageBox.warning(self, "Not Connected", "Connect to a printer first.")
            return

        target_w, target_h = self._get_target_dims()
        if not target_w:
            QMessageBox.warning(self, "No Roll", "Select a roll size.")
            return

        mode_id = self.mode_group.checkedId()
        no_stretch = mode_id == 1
        crop = mode_id == 2
        align = self.align_combo.currentText()
        gamma = self.gamma_spin.value()
        dither = self.dither_cb.isChecked()
        threshold = self.threshold_spin.value()
        density = self.density_spin.value()
        quantity = self.quantity_spin.value()

        # Process all images
        items = []
        for name, img in self._images:
            label = _prepare_label_image(
                img, target_w, target_h, dither, threshold, 0,
                no_stretch, align, gamma, crop)
            items.append((label, density, quantity, 1))

        self.print_btn.setEnabled(False)
        self.print_btn.setText("Printing...")
        self.ble.do_print_batch(items)

    def _on_result(self, op_id, data):
        if op_id in ("print", "print_batch"):
            self.print_btn.setEnabled(True)
            self.print_btn.setText("Print")
            QMessageBox.information(self, "Done", "Print job completed.")

    def _on_error(self, op_id, msg):
        if op_id in ("print", "print_batch"):
            self.print_btn.setEnabled(True)
            self.print_btn.setText("Print")
            QMessageBox.critical(self, "Print Error", msg)

    def _on_printer_connected(self, name):
        # Update gamma default from printer model
        default_gamma = getattr(self.ble.printer, 'DEFAULT_GAMMA', 1.0)
        self.gamma_spin.setValue(default_gamma)

    def _on_printer_disconnected(self):
        pass
