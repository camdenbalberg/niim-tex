"""Print panel — file picker, live B&W preview, settings, and print button."""

import os
import math

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QSlider, QDoubleSpinBox, QSpinBox, QCheckBox, QFileDialog,
    QListWidget, QListWidgetItem, QRadioButton, QButtonGroup, QMessageBox,
    QScrollArea, QSplitter, QApplication, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QImage, QPixmap, QPainter, QWheelEvent, QKeySequence, QShortcut

from PIL import Image

from niim_tex import DPI, LABEL_SIZES, mm_to_px
from PIL import ImageEnhance

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


class ZoomablePreview(QGraphicsView):
    """Image preview widget with auto-fit and manual zoom (Ctrl+scroll, Ctrl+/-)."""

    zoom_changed = pyqtSignal(int)  # emits zoom percentage

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = None
        self._auto_fit = False
        self._zoom = 0.30  # 30% = real-life size on most monitors

        self.setStyleSheet("background-color: #ecf0f1; border: 1px solid #bdc3c7;")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        # Nearest-neighbor — WYSIWYG, matches the saved file exactly
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

        # Keyboard zoom shortcuts
        QShortcut(QKeySequence("Ctrl+="), self, lambda: self._zoom_by(1.25))
        QShortcut(QKeySequence("Ctrl+-"), self, lambda: self._zoom_by(0.8))
        QShortcut(QKeySequence("Ctrl+0"), self, self.fit_to_view)

    def set_pixmap(self, pixmap):
        """Set a new image at the current zoom level."""
        self._scene.clear()
        if pixmap and not pixmap.isNull():
            self._pixmap_item = self._scene.addPixmap(pixmap)
            self._scene.setSceneRect(pixmap.rect().toRectF())
            self.resetTransform()
            self.scale(self._zoom, self._zoom)
        else:
            self._pixmap_item = None

    def fit_to_view(self):
        """Zoom to fit the image in the viewport."""
        if self._pixmap_item:
            self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
            self._zoom = self.transform().m11()
            self._auto_fit = True

    def _zoom_by(self, factor):
        """Apply a relative zoom factor."""
        self._auto_fit = False
        self._zoom *= factor
        self._zoom = max(0.01, min(self._zoom, 20.0))
        self.resetTransform()
        self.scale(self._zoom, self._zoom)
        self.zoom_changed.emit(round(self._zoom * 100))

    def wheelEvent(self, event: QWheelEvent):
        """Ctrl+scroll to zoom, plain scroll to pan."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            factor = 1.15 if delta > 0 else 1 / 1.15
            self._zoom_by(factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def resizeEvent(self, event):
        """Re-fit on resize if auto-fit is active."""
        super().resizeEvent(event)
        if self._auto_fit and self._pixmap_item:
            self.fit_to_view()


class PreviewWorker(QThread):
    """Background thread for image processing (keeps UI responsive).

    The preview shows the GRAYSCALE image (with gamma applied, before
    dithering) because that represents what the print actually looks like
    to the human eye at 300 DPI.  Raw dithered pixels look terrible on a
    monitor — the dots don't visually integrate at screen resolution.
    """
    finished = pyqtSignal(object, str)  # (QPixmap, info_text)

    def __init__(self, image, target_w, target_h, dither, threshold,
                 no_stretch, crop, align, gamma,
                 preview_brightness=1.0, preview_contrast=1.0,
                 preview_sharpness=1.0, parent=None):
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
        self.preview_brightness = preview_brightness
        self.preview_contrast = preview_contrast
        self.preview_sharpness = preview_sharpness

    def run(self):
        try:
            # Standard pipeline (same as print/save — untouched by preview corrections)
            label = _prepare_label_image(
                self.image, self.target_w, self.target_h,
                self.dither, self.threshold, 0,
                self.no_stretch, self.align, self.gamma, self.crop)

            # Apply preview-only corrections to simulate thermal print appearance
            preview = label.convert("L")
            if self.preview_brightness != 1.0:
                preview = ImageEnhance.Brightness(preview).enhance(self.preview_brightness)
            if self.preview_contrast != 1.0:
                preview = ImageEnhance.Contrast(preview).enhance(self.preview_contrast)
            if self.preview_sharpness != 1.0:
                preview = ImageEnhance.Sharpness(preview).enhance(self.preview_sharpness)

            pixmap = pil_to_qpixmap(preview)
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
        self.gamma_spin.setRange(0.01, 10.0)
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
        self.dither_cb.stateChanged.connect(self._on_dither_toggled)
        dither_row.addWidget(self.dither_cb)
        dither_row.addWidget(QLabel("Threshold:"))
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(0, 255)
        self.threshold_spin.setValue(128)
        self.threshold_spin.setEnabled(False)  # only active when dither is off
        self.threshold_spin.valueChanged.connect(self._schedule_preview)
        dither_row.addWidget(self.threshold_spin)
        settings_layout.addLayout(dither_row)

        left_layout.addWidget(settings_group)

        # ── Preview Correction (display-only, does NOT affect print/save) ──
        preview_group = QGroupBox("Preview Correction (display only)")
        preview_layout = QVBoxLayout(preview_group)

        def _slider_row(layout, label, min_val, max_val, default, step, decimals):
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFixedWidth(80)
            row.addWidget(lbl)
            spin = QDoubleSpinBox()
            spin.setRange(min_val, max_val)
            spin.setSingleStep(step)
            spin.setDecimals(decimals)
            spin.setValue(default)
            spin.valueChanged.connect(self._schedule_preview)
            row.addWidget(spin)
            layout.addLayout(row)
            return spin

        self.preview_brightness = _slider_row(preview_layout, "Brightness:", 0.0, 10.0, 1.0, 0.05, 2)
        self.preview_contrast = _slider_row(preview_layout, "Contrast:", 0.0, 10.0, 1.0, 0.05, 2)
        self.preview_sharpness = _slider_row(preview_layout, "Sharpness:", 0.0, 10.0, 1.0, 0.1, 1)

        left_layout.addWidget(preview_group)

        # ── Output settings ──
        output_group = QGroupBox("Output")
        output_layout = QVBoxLayout(output_group)

        dpi_row = QHBoxLayout()
        dpi_row.addWidget(QLabel("DPI:"))
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(1, 9999)
        self.dpi_spin.setValue(300)
        self.dpi_spin.setSingleStep(50)
        self.dpi_spin.valueChanged.connect(self._schedule_preview)
        dpi_row.addWidget(self.dpi_spin)
        output_layout.addLayout(dpi_row)

        left_layout.addWidget(output_group)

        # Quantity
        qty_row = QHBoxLayout()
        qty_row.addWidget(QLabel("Copies:"))
        self.quantity_spin = QSpinBox()
        self.quantity_spin.setRange(1, 99)
        self.quantity_spin.setValue(1)
        qty_row.addWidget(self.quantity_spin)
        qty_row.addStretch()
        output_layout.addLayout(qty_row)

        # Print + Save buttons
        btn_row = QHBoxLayout()

        self.print_btn = QPushButton("Print")
        self.print_btn.setMinimumHeight(40)
        self.print_btn.setStyleSheet(
            "QPushButton { background-color: #27ae60; color: white; "
            "font-size: 14px; font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background-color: #2ecc71; }"
            "QPushButton:disabled { background-color: #95a5a6; }"
        )
        self.print_btn.clicked.connect(self._on_print)
        btn_row.addWidget(self.print_btn)

        self.save_btn = QPushButton("Save")
        self.save_btn.setMinimumHeight(40)
        self.save_btn.setStyleSheet(
            "QPushButton { background-color: #2980b9; color: white; "
            "font-size: 14px; font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background-color: #3498db; }"
        )
        self.save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self.save_btn)

        left_layout.addLayout(btn_row)

        splitter.addWidget(left_scroll)

        # ── Right: Preview ───────────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)

        preview_header = QHBoxLayout()
        preview_header.addWidget(QLabel("Print Preview"))
        preview_header.addStretch()

        # Display rendering controls
        self.render_combo = QComboBox()
        self.render_combo.addItems(["Nearest", "Smooth"])
        self.render_combo.setToolTip("Pixel rendering mode")
        self.render_combo.currentIndexChanged.connect(self._on_render_changed)
        preview_header.addWidget(self.render_combo)

        self.zoom_spin = QSpinBox()
        self.zoom_spin.setRange(1, 500)
        self.zoom_spin.setValue(30)
        self.zoom_spin.setSuffix("%")
        self.zoom_spin.setToolTip("Zoom level — match to physical label for 1:1 comparison")
        self.zoom_spin.setFixedWidth(80)
        self.zoom_spin.valueChanged.connect(self._on_zoom_spin)
        preview_header.addWidget(self.zoom_spin)

        fit_btn = QPushButton("Fit")
        fit_btn.setFixedWidth(35)
        fit_btn.clicked.connect(self._on_zoom_fit)
        preview_header.addWidget(fit_btn)

        right_layout.addLayout(preview_header)

        self.preview_view = ZoomablePreview()
        self.preview_view.zoom_changed.connect(self._sync_zoom_spin)
        right_layout.addWidget(self.preview_view, 1)

        self.info_label = QLabel("Ctrl+Scroll to zoom, Ctrl+0 to fit")
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
        if paths:
            self.load_files(paths)

    def load_files(self, paths):
        """Load image/PDF/tex files into the file list."""
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

                # Auto-detect roll size from image dimensions
                if len(self._images) == 1:
                    self._auto_detect_roll(img, printer_dpi)
            except Exception as e:
                QMessageBox.warning(self, "Open Error", f"Could not open {os.path.basename(p)}:\n{e}")

        if self.file_list.count() > 0 and self.file_list.currentRow() < 0:
            self.file_list.setCurrentRow(0)

    def _auto_detect_roll(self, img, dpi):
        """Try to match image dimensions to a known label size and set the roll combo."""
        from niim_tex import MM_PER_INCH
        w_mm = round(img.width * MM_PER_INCH / dpi)
        h_mm = round(img.height * MM_PER_INCH / dpi)
        # Image could be either orientation — try both
        for name, (tape_w, label_l) in LABEL_SIZES.items():
            printable = min(tape_w, 50) if tape_w > 15 else min(tape_w, 12)
            # Match: (width≈printable, height≈label) or (width≈label, height≈printable)
            if ((abs(w_mm - printable) <= 2 and abs(h_mm - label_l) <= 2) or
                (abs(w_mm - label_l) <= 2 and abs(h_mm - printable) <= 2)):
                idx = self.roll_combo.findData(name)
                if idx >= 0:
                    self.roll_combo.setCurrentIndex(idx)
                    return

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
            return None, None
        tape_w, label_l = LABEL_SIZES[roll]
        dpi = self.dpi_spin.value()
        printable_mm = getattr(self.ble.printer, 'PRINTABLE_HEIGHT_MM', 50) if self.ble.printer else 50
        actual_printable = min(tape_w, printable_mm)
        return mm_to_px(actual_printable, dpi), mm_to_px(label_l, dpi)

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
            no_stretch, crop, align, gamma,
            self.preview_brightness.value(),
            self.preview_contrast.value(),
            self.preview_sharpness.value(), self)
        self._preview_worker.finished.connect(self._on_preview_done)
        self._preview_worker.start()

    def _on_dither_toggled(self, state):
        dither_on = state == Qt.CheckState.Checked.value
        self.threshold_spin.setEnabled(not dither_on)
        self._schedule_preview()

    def _sync_zoom_spin(self, pct):
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(pct)
        self.zoom_spin.blockSignals(False)

    def _on_render_changed(self, idx):
        smooth = idx == 1  # 0=Nearest, 1=Smooth
        self.preview_view.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, smooth)
        self.preview_view.viewport().update()

    def _on_zoom_spin(self, pct):
        self.preview_view._auto_fit = False
        scale = pct / 100.0
        self.preview_view._zoom = scale
        self.preview_view.resetTransform()
        self.preview_view.scale(scale, scale)

    def _on_zoom_fit(self):
        self.preview_view.fit_to_view()
        # Update spin to reflect the fit zoom level
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(round(self.preview_view._zoom * 100))
        self.zoom_spin.blockSignals(False)

    def _on_preview_done(self, pixmap, info):
        if pixmap:
            self.preview_view.set_pixmap(pixmap)
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

        # Process all images (no preview corrections — those are display-only)
        items = []
        for name, img in self._images:
            label = _prepare_label_image(
                img, target_w, target_h, dither, threshold, 0,
                no_stretch, align, gamma, crop)
            items.append((label, density, quantity, 1))

        self.print_btn.setEnabled(False)
        self.print_btn.setText("Printing...")
        self.ble.do_print_batch(items)

    def _on_save(self):
        if not self._images:
            QMessageBox.warning(self, "No Images", "Add images to save first.")
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
        printer_dpi = getattr(self.ble.printer, 'DPI', 300) if self.ble.printer else 300

        if len(self._images) == 1:
            name, img = self._images[0]
            base = os.path.splitext(name)[0]
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Print Image", f"{base}_print.png",
                "PNG (*.png);;All Files (*)")
            if not path:
                return
            label = _prepare_label_image(
                img, target_w, target_h, dither, threshold, 0,
                no_stretch, align, gamma, crop)
            label.save(path, dpi=(printer_dpi, printer_dpi))
            QMessageBox.information(self, "Saved",
                f"Saved {label.width}x{label.height}px @ {printer_dpi} DPI\n{path}")
        else:
            folder = QFileDialog.getExistingDirectory(self, "Save Print Images To")
            if not folder:
                return
            for name, img in self._images:
                base = os.path.splitext(name)[0]
                label = _prepare_label_image(
                    img, target_w, target_h, dither, threshold, 0,
                    no_stretch, align, gamma, crop)
                out = os.path.join(folder, f"{base}_print.png")
                label.save(out, dpi=(printer_dpi, printer_dpi))
            QMessageBox.information(self, "Saved",
                f"Saved {len(self._images)} images to {folder}")

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
