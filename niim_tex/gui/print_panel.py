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
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QRectF, QPointF
from PyQt6.QtGui import (QImage, QPixmap, QPainter, QWheelEvent, QKeySequence,
                         QShortcut, QColor, QPen, QPainterPath)
from PyQt6.QtWidgets import QGraphicsRectItem

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


class CropRect(QGraphicsRectItem):
    """Draggable, resizable crop rectangle with corner/edge handles.

    Drag the body to move. Drag edges or corners to resize (maintains
    aspect ratio). Stays clamped within the image bounds.
    """

    HANDLE_SIZE = 8  # pixels in scene coords

    def __init__(self, rect, bounds, aspect, parent=None):
        super().__init__(rect, parent)
        self._bounds = bounds
        self._aspect = aspect  # w/h ratio to maintain
        self._dragging = None  # None, 'move', or handle name
        self._drag_start = None
        self._rect_start = None
        self._pos_start = None
        self.setPen(QPen(QColor(255, 255, 255), 2, Qt.PenStyle.DashLine))
        from PyQt6.QtGui import QBrush
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(10)

    def _handle_rects(self):
        """Return dict of handle name → QRectF in item coords."""
        r = self.rect()
        s = self.HANDLE_SIZE
        hs = s / 2
        return {
            'tl': QRectF(r.left() - hs, r.top() - hs, s, s),
            'tr': QRectF(r.right() - hs, r.top() - hs, s, s),
            'bl': QRectF(r.left() - hs, r.bottom() - hs, s, s),
            'br': QRectF(r.right() - hs, r.bottom() - hs, s, s),
        }

    def _hit_handle(self, pos):
        for name, hr in self._handle_rects().items():
            if hr.contains(pos):
                return name
        return None

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        # Draw corner handles
        painter.setBrush(QColor(255, 255, 255))
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        for hr in self._handle_rects().values():
            painter.drawRect(hr)

    def hoverMoveEvent(self, event):
        handle = self._hit_handle(event.pos())
        if handle in ('tl', 'br'):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif handle in ('tr', 'bl'):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif self.rect().contains(event.pos()):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._hit_handle(event.pos())
            if handle:
                self._dragging = handle
            elif self.rect().contains(event.pos()):
                self._dragging = 'move'
            self._drag_start = event.scenePos()
            self._rect_start = QRectF(self.rect())
            self._pos_start = QPointF(self.pos())
            event.accept()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        delta = event.scenePos() - self._drag_start

        if self._dragging == 'move':
            new_pos = self._pos_start + delta
            r = self.rect()
            x = max(self._bounds.x(), min(new_pos.x(), self._bounds.right() - r.width()))
            y = max(self._bounds.y(), min(new_pos.y(), self._bounds.bottom() - r.height()))
            self.setPos(x, y)
        else:
            # Resize from corner, maintaining aspect ratio
            rs = self._rect_start
            if self._dragging == 'br':
                new_w = max(20, rs.width() + delta.x())
                new_h = new_w / self._aspect
                self.setRect(QRectF(0, 0, new_w, new_h))
            elif self._dragging == 'tl':
                new_w = max(20, rs.width() - delta.x())
                new_h = new_w / self._aspect
                self.setRect(QRectF(0, 0, new_w, new_h))
                self.setPos(self._pos_start.x() + rs.width() - new_w,
                           self._pos_start.y() + rs.height() - new_h)
            elif self._dragging == 'tr':
                new_w = max(20, rs.width() + delta.x())
                new_h = new_w / self._aspect
                self.setRect(QRectF(0, 0, new_w, new_h))
                self.setPos(self._pos_start.x(),
                           self._pos_start.y() + rs.height() - new_h)
            elif self._dragging == 'bl':
                new_w = max(20, rs.width() - delta.x())
                new_h = new_w / self._aspect
                self.setRect(QRectF(0, 0, new_w, new_h))
                self.setPos(self._pos_start.x() + rs.width() - new_w,
                           self._pos_start.y())

            # Clamp to bounds
            r = self.rect()
            p = self.pos()
            if p.x() < self._bounds.x():
                self.setPos(self._bounds.x(), p.y())
            if p.y() < self._bounds.y():
                self.setPos(self.pos().x(), self._bounds.y())
            if p.x() + r.width() > self._bounds.right():
                cw = self._bounds.right() - self.pos().x()
                self.setRect(QRectF(0, 0, cw, cw / self._aspect))
            if p.y() + r.height() > self._bounds.bottom():
                ch = self._bounds.bottom() - self.pos().y()
                self.setRect(QRectF(0, 0, ch * self._aspect, ch))

        event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = None
        event.accept()


class CropOverlay(QGraphicsRectItem):
    """Semi-transparent darkening outside the crop area."""

    def __init__(self, scene_rect, parent=None):
        super().__init__(scene_rect, parent)
        self.setBrush(QColor(0, 0, 0, 120))
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setZValue(5)
        self._hole = None

    def set_hole(self, rect):
        """Set the clear area (crop rectangle)."""
        self._hole = rect

    def paint(self, painter, option, widget=None):
        if self._hole:
            path = QPainterPath()
            path.addRect(self.rect())
            path.addRect(self._hole)
            painter.setBrush(self.brush())
            painter.setPen(self.pen())
            painter.drawPath(path)
        else:
            super().paint(painter, option, widget)


class CropView(QGraphicsView):
    """Interactive crop editor: drag a rectangle on the source image."""

    crop_changed = pyqtSignal(float, float, float, float)  # x, y, w, h as fractions

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = None
        self._crop_rect = None
        self._overlay = None
        self._aspect = 1.0
        self._img_w = 0
        self._img_h = 0

        self.setStyleSheet("background-color: #2c3e50; border: 1px solid #bdc3c7;")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    def set_image(self, pil_image, target_w, target_h):
        """Show the source image with a crop rectangle of the target aspect ratio."""
        self._scene.clear()
        if pil_image is None:
            return

        # Auto-rotate source to match target orientation
        img = pil_image
        img_landscape = img.width > img.height
        target_landscape = target_w > target_h
        if img_landscape != target_landscape and img.width != img.height:
            img = img.rotate(-90, expand=True)

        pixmap = pil_to_qpixmap(img)
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._img_w = pixmap.width()
        self._img_h = pixmap.height()
        self._aspect = target_w / target_h

        img_rect = pixmap.rect().toRectF()
        self._scene.setSceneRect(img_rect)

        # Calculate initial crop rect (centered, max size that fits)
        scale = max(target_w / self._img_w, target_h / self._img_h)
        # Crop rect in image coordinates
        cw = target_w / scale
        ch = target_h / scale
        cx = (self._img_w - cw) / 2
        cy = (self._img_h - ch) / 2

        from PyQt6.QtCore import QRectF
        crop_r = QRectF(0, 0, cw, ch)
        self._crop_rect = CropRect(crop_r, img_rect, self._aspect)
        self._crop_rect.setPos(cx, cy)
        self._scene.addItem(self._crop_rect)

        # Dark overlay
        self._overlay = CropOverlay(img_rect)
        self._scene.addItem(self._overlay)
        self._update_overlay()

        # Connect movement
        self._crop_rect.setFlag(
            QGraphicsRectItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)

        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

        # Timer to track crop rect movement
        self._track_timer = QTimer(self)
        self._track_timer.setInterval(100)
        self._track_timer.timeout.connect(self._on_crop_moved)
        self._track_timer.start()

    def _update_overlay(self):
        if self._overlay and self._crop_rect:
            r = self._crop_rect.rect()
            pos = self._crop_rect.pos()
            from PyQt6.QtCore import QRectF
            self._overlay.set_hole(QRectF(pos.x(), pos.y(), r.width(), r.height()))
            self._overlay.update()

    def _on_crop_moved(self):
        if not self._crop_rect or not self._img_w:
            return
        self._update_overlay()
        pos = self._crop_rect.pos()
        r = self._crop_rect.rect()
        # Emit crop as fractions of image size
        self.crop_changed.emit(
            pos.x() / self._img_w,
            pos.y() / self._img_h,
            r.width() / self._img_w,
            r.height() / self._img_h,
        )

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            factor = 1.15 if delta > 0 else 1 / 1.15
            self.scale(factor, factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def get_crop_box(self):
        """Return (left, top, right, bottom) in image pixel coordinates."""
        if not self._crop_rect:
            return None
        pos = self._crop_rect.pos()
        r = self._crop_rect.rect()
        return (int(pos.x()), int(pos.y()),
                int(pos.x() + r.width()), int(pos.y() + r.height()))


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
                 crop_x=0, crop_y=0,
                 gamma_offset=-0.30, dot_spread=1.3, darken=4.20,
                 black_pt=25.0, parent=None):
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
        self.crop_x = crop_x
        self.crop_y = crop_y
        self.gamma_offset = gamma_offset
        self.dot_spread = dot_spread
        self.darken = darken
        self.black_pt = black_pt

    def run(self):
        try:
            from PIL import ImageFilter

            img = self.image

            # Pipeline with preview gamma offset applied
            preview_gamma = max(0.01, self.gamma + self.gamma_offset)
            label = _prepare_label_image(
                img, self.target_w, self.target_h,
                self.dither, self.threshold, 0,
                self.no_stretch, self.align, preview_gamma, self.crop)

            # Display-only corrections to simulate thermal print appearance
            preview = label.convert("L")

            if self.dot_spread > 0:
                preview = preview.filter(ImageFilter.GaussianBlur(radius=self.dot_spread))

            bp = int(self.black_pt)
            if self.darken != 1.0 or bp > 0:
                lut = []
                for i in range(256):
                    v = int(255 * (i / 255) ** self.darken) if self.darken != 1.0 else i
                    if v <= bp:
                        v = 0
                    lut.append(min(255, v))
                preview = preview.point(lut)

            pixmap = pil_to_qpixmap(preview)
            info = (f"{label.width}x{label.height}px | "
                    f"g={preview_gamma:.2f} blur={self.dot_spread} "
                    f"dark={self.darken} bp={bp}")
            self.finished.emit(pixmap, info)
        except Exception as e:
            self.finished.emit(None, f"Error: {e}")


class PrintPanel(QWidget):
    def __init__(self, ble_thread, parent=None):
        super().__init__(parent)
        self.ble = ble_thread
        self._images = []       # [(filename, PIL.Image), ...]
        self._file_paths = []   # full paths for settings persistence
        self._current_idx = 0
        self._crop_fractions = None  # (x, y, w, h) as fractions when using interactive crop
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
        self.add_files_btn = QPushButton("Files...")
        self.add_files_btn.clicked.connect(self._on_add_files)
        btn_row.addWidget(self.add_files_btn)
        self.add_folder_btn = QPushButton("Folder...")
        self.add_folder_btn.clicked.connect(self._on_add_folder)
        btn_row.addWidget(self.add_folder_btn)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(self.remove_btn)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        btn_row.addWidget(self.clear_btn)
        file_layout.addLayout(btn_row)
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
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

        # Crop offset (shift the crop window when using Crop mode)
        crop_x_row = QHBoxLayout()
        crop_x_row.addWidget(QLabel("Crop X:"))
        self.crop_x_spin = QSpinBox()
        self.crop_x_spin.setRange(-500, 500)
        self.crop_x_spin.setValue(0)
        self.crop_x_spin.setSuffix(" px")
        self.crop_x_spin.valueChanged.connect(self._schedule_preview)
        crop_x_row.addWidget(self.crop_x_spin)
        crop_x_row.addStretch()
        settings_layout.addLayout(crop_x_row)

        crop_y_row = QHBoxLayout()
        crop_y_row.addWidget(QLabel("Crop Y:"))
        self.crop_y_spin = QSpinBox()
        self.crop_y_spin.setRange(-500, 500)
        self.crop_y_spin.setValue(0)
        self.crop_y_spin.setSuffix(" px")
        self.crop_y_spin.valueChanged.connect(self._schedule_preview)
        crop_y_row.addWidget(self.crop_y_spin)
        crop_y_row.addStretch()
        settings_layout.addLayout(crop_y_row)

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

        self.preview_gamma_offset = _slider_row(preview_layout, "Gamma ofs:", -1.0, 1.0, -0.30, 0.01, 2)
        self.preview_dot_spread = _slider_row(preview_layout, "Dot spread:", 0.0, 10.0, 1.3, 0.1, 1)
        self.preview_darken = _slider_row(preview_layout, "Darken:", 0.0, 10.0, 4.20, 0.05, 2)
        self.preview_black_pt = _slider_row(preview_layout, "Black point:", 0.0, 255.0, 49.0, 1.0, 0)

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
        self.render_combo.addItems(["Print sim", "Raw pixels", "Crop editor"])
        self.render_combo.setToolTip("Print sim = thermal print look\nRaw = exact output\nCrop editor = position the crop")
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

        self.crop_view = CropView()
        self.crop_view.crop_changed.connect(self._on_interactive_crop)
        self.crop_view.hide()

        from PyQt6.QtWidgets import QStackedWidget
        self._preview_stack = QStackedWidget()
        self._preview_stack.addWidget(self.preview_view)   # index 0
        self._preview_stack.addWidget(self.crop_view)       # index 1
        right_layout.addWidget(self._preview_stack, 1)

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

    def _on_add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Image Folder")
        if not folder:
            return
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif",
                      ".webp", ".avif", ".heif", ".heic", ".pdf"}
        paths = []
        for name in sorted(os.listdir(folder)):
            if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                paths.append(os.path.join(folder, name))
        if paths:
            self.load_files(paths)
        else:
            QMessageBox.warning(self, "No Images", f"No image files found in {folder}")

    def load_files(self, paths):
        """Add files to the list (lazy — images loaded on demand, not upfront)."""
        for p in paths:
            abs_p = os.path.abspath(p)
            if abs_p in self._file_paths:
                continue  # skip duplicates
            name = os.path.basename(p)
            self._images.append((name, None))  # None = not loaded yet
            self._file_paths.append(abs_p)
            self.file_list.addItem(name)

        # Auto-detect roll from first file
        if len(self._images) >= 1 and self._images[0][1] is None:
            try:
                img = self._load_image(self._file_paths[0])
                self._images[0] = (self._images[0][0], img)
                printer_dpi = getattr(self.ble.printer, 'DPI', DPI) if self.ble.printer else 300
                self._auto_detect_roll(img, printer_dpi)
            except Exception:
                pass

        if self.file_list.count() > 0 and self.file_list.currentRow() < 0:
            self.file_list.setCurrentRow(0)

    def _load_image(self, path):
        """Load and convert a single image (called on demand)."""
        printer_dpi = getattr(self.ble.printer, 'DPI', DPI) if self.ble.printer else 300
        if path.lower().endswith('.tex'):
            rotate = 90
            printer_printable = getattr(self.ble.printer, 'PRINTABLE_HEIGHT_MM', 12) if self.ble.printer else 50
            from niim_tex.cli import parse_geometry_from_tex, find_label_size_for_geometry
            pw, ph = parse_geometry_from_tex(path)
            if pw and ph and pw < ph:
                rotate = 0
            elif pw and ph:
                _, _, ll = find_label_size_for_geometry(pw, ph)
                if ll and printer_printable >= ll:
                    rotate = 0
            png_path, _ = compile_tex_to_png(path, dpi=printer_dpi, rotate=rotate)
            return Image.open(png_path).convert("L")
        else:
            return open_image(path, dpi=printer_dpi).convert("L")

    def _get_image(self, idx):
        """Get image at index, loading lazily if needed."""
        name, img = self._images[idx]
        if img is None:
            try:
                img = self._load_image(self._file_paths[idx])
                self._images[idx] = (name, img)
            except Exception as e:
                QMessageBox.warning(self, "Load Error",
                    f"Could not load {name}:\n{e}")
                return None
        return img

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

    def _on_remove(self):
        row = self.file_list.currentRow()
        if row >= 0 and row < len(self._images):
            self._images.pop(row)
            self._file_paths.pop(row)
            self.file_list.takeItem(row)
            if not self._images:
                self.preview_view.set_pixmap(QPixmap())
                self.info_label.setText("")

    def _on_clear(self):
        self._images.clear()
        self._file_paths.clear()
        self.file_list.clear()
        self._current_idx = -1
        self.preview_view.set_pixmap(QPixmap())
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

        img = self._get_image(self._current_idx)
        if img is None:
            return
        mode_id = self.mode_group.checkedId()
        no_stretch = mode_id == 1
        crop = mode_id == 2
        render_idx = self.render_combo.currentIndex()

        # Crop editor view (index 2)
        if render_idx == 2 and crop:
            self._preview_stack.setCurrentIndex(1)
            self.crop_view.set_image(img, target_w, target_h)
            self.info_label.setText("Drag the crop rectangle to reposition")
            return

        # For Print sim / Raw, if crop mode is active, apply interactive crop
        if crop and self._crop_fractions:
            img = self._apply_interactive_crop(img)
            crop = False
            no_stretch = False

        self._preview_stack.setCurrentIndex(0)
        align = self.align_combo.currentText()
        gamma = self.gamma_spin.value()
        dither = self.dither_cb.isChecked()
        threshold = self.threshold_spin.value()

        raw_mode = render_idx == 1
        if raw_mode:
            g_ofs, d_spr, dark, bp = 0.0, 0.0, 1.0, 0.0
        else:
            g_ofs = self.preview_gamma_offset.value()
            d_spr = self.preview_dot_spread.value()
            dark = self.preview_darken.value()
            bp = self.preview_black_pt.value()

        # Process synchronously (fast enough, avoids thread signal issues)
        try:
            from PIL import ImageFilter

            preview_gamma = max(0.01, gamma + g_ofs)
            label = _prepare_label_image(
                img, target_w, target_h, dither, threshold, 0,
                no_stretch, align, preview_gamma, False)

            preview = label.convert("L")

            if d_spr > 0:
                preview = preview.filter(ImageFilter.GaussianBlur(radius=d_spr))

            ibp = int(bp)
            if dark != 1.0 or ibp > 0:
                lut = []
                for i in range(256):
                    v = int(255 * (i / 255) ** dark) if dark != 1.0 else i
                    if v <= ibp:
                        v = 0
                    lut.append(min(255, v))
                preview = preview.point(lut)

            pixmap = pil_to_qpixmap(preview)
            self.preview_view.set_pixmap(pixmap)
            self.info_label.setText(
                f"{label.width}x{label.height}px | "
                f"g={preview_gamma:.2f} blur={d_spr} dark={dark} bp={ibp}")
        except Exception as e:
            self.info_label.setText(f"Error: {e}")

    def _on_dither_toggled(self, state):
        dither_on = state == Qt.CheckState.Checked.value
        self.threshold_spin.setEnabled(not dither_on)
        self._schedule_preview()

    def _on_interactive_crop(self, x, y, w, h):
        """Called when user drags the crop rectangle."""
        self._crop_fractions = (x, y, w, h)

    def _apply_interactive_crop(self, img):
        """Crop the source image using the interactive crop rectangle."""
        if not self._crop_fractions:
            return img
        # Auto-rotate source to match target like CropView does
        target_w, target_h = self._get_target_dims()
        if target_w:
            img_landscape = img.width > img.height
            target_landscape = target_w > target_h
            if img_landscape != target_landscape and img.width != img.height:
                img = img.rotate(-90, expand=True)
        x, y, w, h = self._crop_fractions
        left = int(x * img.width)
        top = int(y * img.height)
        right = int((x + w) * img.width)
        bottom = int((y + h) * img.height)
        return img.crop((left, top, right, bottom))

    def _sync_zoom_spin(self, pct):
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(pct)
        self.zoom_spin.blockSignals(False)

    def _on_render_changed(self, idx):
        # 0=Print sim, 1=Raw pixels, 2=Crop editor
        self.preview_view.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, idx == 0)
        self._schedule_preview()

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

        # Use selected items, or all if none selected
        selected_rows = [idx.row() for idx in self.file_list.selectedIndexes()]
        if not selected_rows:
            selected_rows = list(range(len(self._images)))

        items = []
        for i in selected_rows:
            img = self._get_image(i)
            if img is None:
                continue
            if crop and self._crop_fractions:
                # Interactive crop — pre-crop then stretch to fill
                src = self._apply_interactive_crop(img)
                label = _prepare_label_image(
                    src, target_w, target_h, dither, threshold, 0,
                    False, align, gamma, False)
            else:
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

        def _process(img):
            if crop and self._crop_fractions:
                src = self._apply_interactive_crop(img)
                return _prepare_label_image(
                    src, target_w, target_h, dither, threshold, 0,
                    False, align, gamma, False)
            return _prepare_label_image(
                img, target_w, target_h, dither, threshold, 0,
                no_stretch, align, gamma, crop)

        if len(self._images) == 1:
            name = self._images[0][0]
            img = self._get_image(0)
            if img is None:
                return
            base = os.path.splitext(name)[0]
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Print Image", f"{base}_print.png",
                "PNG (*.png);;All Files (*)")
            if not path:
                return
            label = _process(img)
            label.save(path, dpi=(printer_dpi, printer_dpi))
            QMessageBox.information(self, "Saved",
                f"Saved {label.width}x{label.height}px @ {printer_dpi} DPI\n{path}")
        else:
            folder = QFileDialog.getExistingDirectory(self, "Save Print Images To")
            if not folder:
                return
            for i, (name, _) in enumerate(self._images):
                img = self._get_image(i)
                if img is None:
                    continue
                base = os.path.splitext(name)[0]
                label = _process(img)
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

    @staticmethod
    def _apply_crop_offset(img, crop, crop_x, crop_y):
        """Shift the image to offset the crop window."""
        if not crop or (crop_x == 0 and crop_y == 0):
            return img
        from PIL import ImageOps as _IOps
        pl = max(0, crop_x)
        pr = max(0, -crop_x)
        pt = max(0, crop_y)
        pb = max(0, -crop_y)
        return _IOps.expand(img, border=(pl, pt, pr, pb), fill=128)

    # ── Settings persistence ─────────────────────────────────────────

    def save_settings(self, s):
        """Save all panel state to QSettings."""
        # Files
        paths = []
        for name, img in self._images:
            # Store the original path if we have it
            paths.append(name)
        s.setValue("print/file_paths", self._file_paths if hasattr(self, '_file_paths') else [])

        # Label settings
        s.setValue("print/roll", self.roll_combo.currentData())
        s.setValue("print/density", self.density_spin.value())
        s.setValue("print/gamma", self.gamma_spin.value())
        s.setValue("print/resize_mode", self.mode_group.checkedId())
        s.setValue("print/align", self.align_combo.currentText())
        s.setValue("print/dither", self.dither_cb.isChecked())
        s.setValue("print/threshold", self.threshold_spin.value())
        s.setValue("print/quantity", self.quantity_spin.value())
        s.setValue("print/dpi", self.dpi_spin.value())
        s.setValue("print/crop_x", self.crop_x_spin.value())
        s.setValue("print/crop_y", self.crop_y_spin.value())

        # Preview corrections
        s.setValue("preview/gamma_offset", self.preview_gamma_offset.value())
        s.setValue("preview/dot_spread", self.preview_dot_spread.value())
        s.setValue("preview/darken", self.preview_darken.value())
        s.setValue("preview/black_pt", self.preview_black_pt.value())

        # Display
        s.setValue("preview/render", self.render_combo.currentIndex())
        s.setValue("preview/zoom", self.zoom_spin.value())

    def restore_settings(self, s):
        """Restore all panel state from QSettings."""
        # Label settings
        roll = s.value("print/roll")
        if roll:
            idx = self.roll_combo.findData(roll)
            if idx >= 0:
                self.roll_combo.setCurrentIndex(idx)

        self.density_spin.setValue(int(s.value("print/density", 3)))
        self.gamma_spin.setValue(float(s.value("print/gamma", 0.55)))

        mode = int(s.value("print/resize_mode", 0))
        btn = self.mode_group.button(mode)
        if btn:
            btn.setChecked(True)

        align = s.value("print/align", "center")
        idx = self.align_combo.findText(align)
        if idx >= 0:
            self.align_combo.setCurrentIndex(idx)

        self.dither_cb.setChecked(s.value("print/dither", True, type=bool))
        self.threshold_spin.setValue(int(s.value("print/threshold", 128)))
        self.quantity_spin.setValue(int(s.value("print/quantity", 1)))
        self.crop_x_spin.setValue(int(s.value("print/crop_x", 0)))
        self.crop_y_spin.setValue(int(s.value("print/crop_y", 0)))
        self.dpi_spin.setValue(int(s.value("print/dpi", 300)))

        # Preview corrections
        self.preview_gamma_offset.setValue(float(s.value("preview/gamma_offset", -0.30)))
        self.preview_dot_spread.setValue(float(s.value("preview/dot_spread", 1.3)))
        self.preview_darken.setValue(float(s.value("preview/darken", 4.20)))
        self.preview_black_pt.setValue(float(s.value("preview/black_pt", 49.0)))

        # Display
        self.render_combo.setCurrentIndex(int(s.value("preview/render", 0)))
        self.zoom_spin.setValue(int(s.value("preview/zoom", 30)))

        # Files — load after GUI is visible (deferred)
        paths = s.value("print/file_paths", [])
        if paths and isinstance(paths, list):
            valid = [p for p in paths if os.path.isfile(p)]
            if valid:
                QTimer.singleShot(500, lambda: self.load_files(valid))
