import sys
import os
import io
import zipfile
from enum import Enum, auto

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QFileDialog, QMessageBox,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem,
    QSplitter, QMenu, QSizePolicy, QStatusBar
)
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QPen, QBrush, QColor, QTransform, QCursor, QAction,
    QKeyEvent # Added for key press event
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QSize # Added QSize

from PIL import Image

# Helper function to convert Pillow Image to QImage (same as before)
def pillow_to_qimage(pil_img):
    if pil_img.mode == "RGB": pass
    elif pil_img.mode == "RGBA": pass
    else: pil_img = pil_img.convert("RGBA")
    data = pil_img.tobytes("raw", pil_img.mode)
    if pil_img.mode == "RGB": qimage_format = QImage.Format.Format_RGB888
    elif pil_img.mode == "RGBA": qimage_format = QImage.Format.Format_RGBA8888
    else: qimage_format = QImage.Format.Format_RGBA8888
    qimage = QImage(data, pil_img.width, pil_img.height, qimage_format)
    return qimage.copy()

# --- Enums and Data Classes (Mostly Unchanged) ---
class InteractionMode(Enum):
    NONE = auto()
    PANNING = auto()
    DRAWING = auto()
    MOVING = auto()
    RESIZING = auto()

class HandlePosition(Enum):
    NONE = auto()
    TOP_LEFT = auto()
    TOP_RIGHT = auto()
    BOTTOM_LEFT = auto()
    BOTTOM_RIGHT = auto()

class CropInfo:
    _next_id = 1
    def __init__(self, rect_item: QGraphicsRectItem):
        self.id = CropInfo._next_id
        CropInfo._next_id += 1
        self.rect_item = rect_item

    def get_rect_image_coords(self) -> QRectF:
        # Combine item's position (top-left) and its relative rect
        return QRectF(self.rect_item.pos(), self.rect_item.rect().size())

    def set_rect_image_coords(self, scene_rect: QRectF):
        # Set item's position and its relative rect based on scene rect
        self.rect_item.setPos(scene_rect.topLeft())
        self.rect_item.setRect(QRectF(QPointF(0, 0), scene_rect.size()))

    def __str__(self):
        rect = self.get_rect_image_coords()
        return f"Crop {self.id}: (W: {rect.width():.0f}, H: {rect.height():.0f})"

# --- ResizeHandleItem (Unchanged) ---
class ResizeHandleItem(QGraphicsRectItem):
    def __init__(self, parent_crop_item: QGraphicsRectItem, position: HandlePosition, size: float = 8.0):
        super().__init__(-size / 2, -size / 2, size, size, parent=parent_crop_item) # Centered rect
        self.parent_crop_item = parent_crop_item
        self.position = position
        self.handle_size = size
        self.setBrush(QBrush(QColor(0, 123, 255, 220)))
        self.setPen(QPen(QColor(255, 255, 255, 200), 1.0))
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setZValue(10)
        self.update_position()

    def update_position(self):
        # Position is relative to the parent item's origin (0,0)
        parent_rect = self.parent_crop_item.rect() # This rect is relative to parent's pos()
        center_x, center_y = 0, 0
        if self.position == HandlePosition.TOP_LEFT: center_x, center_y = parent_rect.left(), parent_rect.top() # Should be 0, 0
        elif self.position == HandlePosition.TOP_RIGHT: center_x, center_y = parent_rect.right(), parent_rect.top()
        elif self.position == HandlePosition.BOTTOM_LEFT: center_x, center_y = parent_rect.left(), parent_rect.bottom()
        elif self.position == HandlePosition.BOTTOM_RIGHT: center_x, center_y = parent_rect.right(), parent_rect.bottom()
        # Set position *relative to the parent item*
        self.setPos(center_x, center_y)

# --- Custom Graphics View (Updates for Resize Fix and Delete Key) ---
class CropGraphicsView(QGraphicsView):
    crop_selected_signal = pyqtSignal(object)
    crops_updated_signal = pyqtSignal()
    status_message_signal = pyqtSignal(str) # Signal for status bar messages

    def __init__(self, scene: QGraphicsScene, parent=None):
        super().__init__(scene, parent)
        self.parent_window: 'MainWindow' = parent # Type hint for parent window
        self._mode = InteractionMode.NONE
        self._start_pan_pos = QPointF()
        self._start_scene_pos = QPointF()
        self._start_interaction_rect = QRectF() # Store rect at start of move/resize
        self._current_temp_rect_item: QGraphicsRectItem | None = None
        self._selected_crop_info: CropInfo | None = None
        self._interaction_crop_info: CropInfo | None = None
        self._active_handle: ResizeHandleItem | None = None
        self._handle_items: list[ResizeHandleItem] = []

        # Pens and brushes (minor tweaks)
        self.crop_pen = QPen(QColor(220, 50, 50, 180), 1.5) # Slightly different red
        self.crop_pen.setCosmetic(True)
        self.selected_crop_pen = QPen(QColor(50, 150, 255, 200), 2.0) # Slightly different blue, thinner
        self.selected_crop_pen.setCosmetic(True)
        self.temp_draw_pen = QPen(QColor(50, 150, 255, 200), 1.5, Qt.PenStyle.DashLine)
        self.temp_draw_pen.setCosmetic(True)

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus) # Needed to receive key events

    # --- Getters and Setters (Minor changes) ---
    def get_image_item(self) -> QGraphicsPixmapItem | None:
        items = self.scene().items()
        # Find the first (usually only) QGraphicsPixmapItem
        return next((item for item in items if isinstance(item, QGraphicsPixmapItem)), None)

    def set_selected_crop(self, crop_info: CropInfo | None):
        if self._selected_crop_info == crop_info: return
        if self._selected_crop_info:
            self._selected_crop_info.rect_item.setPen(self.crop_pen)
            self._selected_crop_info.rect_item.setZValue(0)
            for handle in self._handle_items: self.scene().removeItem(handle)
            self._handle_items.clear()
        self._selected_crop_info = crop_info
        if self._selected_crop_info:
            self._selected_crop_info.rect_item.setPen(self.selected_crop_pen)
            self._selected_crop_info.rect_item.setZValue(1)
            parent_rect_item = self._selected_crop_info.rect_item
            for pos_enum in HandlePosition:
                if pos_enum != HandlePosition.NONE:
                    handle = ResizeHandleItem(parent_rect_item, pos_enum)
                    self._handle_items.append(handle)
            # Ensure handles are positioned correctly initially
            QApplication.processEvents() # Allow item positions to settle before updating handles
            self.update_handle_positions()
        self.crop_selected_signal.emit(self._selected_crop_info)
        self.viewport().update()

    def update_handle_positions(self):
        for handle in self._handle_items:
            handle.update_position()

    # --- Events (Key Press added, Resize logic fixed in Mouse Move) ---
    def mousePressEvent(self, event):
        image_item = self.get_image_item()
        if not image_item: super().mousePressEvent(event); return

        self._start_scene_pos = self.mapToScene(event.pos())

        # Panning Start
        if event.button() == Qt.MouseButton.MiddleButton or \
           (event.button() == Qt.MouseButton.LeftButton and QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier):
            self._mode = InteractionMode.PANNING
            self._start_pan_pos = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept(); return

        if event.button() == Qt.MouseButton.LeftButton:
            # Check handles first
            clicked_item = self.itemAt(event.pos())
            if isinstance(clicked_item, ResizeHandleItem) and self._selected_crop_info and clicked_item in self._handle_items:
                self._mode = InteractionMode.RESIZING
                self._interaction_crop_info = self._selected_crop_info
                self._active_handle = clicked_item
                self._start_interaction_rect = self._interaction_crop_info.get_rect_image_coords() # Store initial rect
                self.setCursor(self.get_resize_cursor(clicked_item.position))
                event.accept(); return

            # Check crop bodies
            # Re-check item at pos, as it might be different from handle check if overlapping
            clicked_item_body = self.scene().itemAt(self._start_scene_pos, self.transform()) # Check scene pos
            if isinstance(clicked_item_body, QGraphicsRectItem) and clicked_item_body not in self._handle_items and clicked_item_body != self._current_temp_rect_item:
                found_crop = next((info for info in self.parent_window.crops if info.rect_item == clicked_item_body), None)
                if found_crop:
                    self._mode = InteractionMode.MOVING
                    self.set_selected_crop(found_crop)
                    self._interaction_crop_info = found_crop
                    self._start_interaction_rect = found_crop.get_rect_image_coords() # Store initial rect
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                    event.accept(); return

            # Start drawing if on image
            img_bounds = image_item.sceneBoundingRect()
            if img_bounds.contains(self._start_scene_pos):
                self._mode = InteractionMode.DRAWING
                self.set_selected_crop(None)
                if self._current_temp_rect_item: self.scene().removeItem(self._current_temp_rect_item)
                self._current_temp_rect_item = QGraphicsRectItem(QRectF(self._start_scene_pos, QSize(0,0)))
                self._current_temp_rect_item.setPen(self.temp_draw_pen)
                self.scene().addItem(self._current_temp_rect_item)
                self.setCursor(Qt.CursorShape.CrossCursor)
                event.accept(); return

        super().mousePressEvent(event) # Pass event up

    def mouseMoveEvent(self, event):
        current_scene_pos = self.mapToScene(event.pos())
        image_item = self.get_image_item()
        if not image_item: super().mouseMoveEvent(event); return

        img_rect_scene = image_item.sceneBoundingRect()

        # Emit status message with image coordinates
        status_pos = QPointF(max(0, min(current_scene_pos.x(), img_rect_scene.right())),
                             max(0, min(current_scene_pos.y(), img_rect_scene.bottom()))) # Clamped coords
        self.status_message_signal.emit(f"Image Coords: ({status_pos.x():.0f}, {status_pos.y():.0f})")


        if self._mode == InteractionMode.PANNING:
            delta = event.pos() - self._start_pan_pos
            self.translate(delta.x(), delta.y())
            self._start_pan_pos = event.pos()
            event.accept(); return

        if self._mode == InteractionMode.DRAWING and self._current_temp_rect_item:
            rect_sc = QRectF(self._start_scene_pos, current_scene_pos).normalized()
            clamped_rect = rect_sc.intersected(img_rect_scene)
            self._current_temp_rect_item.setRect(clamped_rect)
            event.accept(); return

        if self._mode == InteractionMode.MOVING and self._interaction_crop_info:
            delta = current_scene_pos - self._start_scene_pos
            original_rect_scene = self._start_interaction_rect # Use stored start rect
            new_top_left_scene = original_rect_scene.topLeft() + delta

            # Clamp movement
            if new_top_left_scene.x() < img_rect_scene.left(): new_top_left_scene.setX(img_rect_scene.left())
            if new_top_left_scene.y() < img_rect_scene.top(): new_top_left_scene.setY(img_rect_scene.top())
            if new_top_left_scene.x() + original_rect_scene.width() > img_rect_scene.right():
                new_top_left_scene.setX(img_rect_scene.right() - original_rect_scene.width())
            if new_top_left_scene.y() + original_rect_scene.height() > img_rect_scene.bottom():
                 new_top_left_scene.setY(img_rect_scene.bottom() - original_rect_scene.height())

            # Apply the move by setting the item's position
            self._interaction_crop_info.rect_item.setPos(new_top_left_scene)
            self.crops_updated_signal.emit()
            event.accept(); return

        # --- FIXED RESIZE LOGIC ---
        if self._mode == InteractionMode.RESIZING and self._interaction_crop_info and self._active_handle:
            new_rect_scene = self.calculate_resized_rect(
                self._start_interaction_rect, # Use stored start rect
                self._start_scene_pos, # Where drag started
                current_scene_pos,     # Where mouse is now
                self._active_handle.position,
                img_rect_scene # Clamp boundary
            )
            # Update the CropInfo object which updates the underlying QGraphicsItem
            self._interaction_crop_info.set_rect_image_coords(new_rect_scene)
            self.update_handle_positions()
            self.crops_updated_signal.emit()
            event.accept(); return
        # --- END FIXED RESIZE LOGIC ---

        if self._mode == InteractionMode.NONE:
            self.update_hover_cursor(event.pos())

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._mode == InteractionMode.NONE: super().mouseReleaseEvent(event); return

        # Clear status bar after interaction
        self.status_message_signal.emit("")

        if event.button() == Qt.MouseButton.MiddleButton and self._mode == InteractionMode.PANNING:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._mode = InteractionMode.NONE
            event.accept(); return

        if event.button() == Qt.MouseButton.LeftButton:
            if self._mode == InteractionMode.DRAWING and self._current_temp_rect_item:
                final_rect_scene = self._current_temp_rect_item.rect()
                self.scene().removeItem(self._current_temp_rect_item)
                self._current_temp_rect_item = None
                min_size = 5
                if final_rect_scene.width() > min_size and final_rect_scene.height() > min_size:
                    # Create permanent item at (0,0) with correct size
                    crop_item = QGraphicsRectItem(QRectF(QPointF(0,0), final_rect_scene.size()))
                    crop_item.setPos(final_rect_scene.topLeft()) # Set position
                    crop_item.setPen(self.crop_pen)
                    self.scene().addItem(crop_item)
                    new_crop_info = CropInfo(crop_item)
                    self.parent_window.crops.append(new_crop_info)
                    self.parent_window.update_crop_list()
                    self.set_selected_crop(new_crop_info)
                else:
                    self.set_selected_crop(None)

            elif self._mode == InteractionMode.MOVING or self._mode == InteractionMode.RESIZING:
                 # Ensure final list text reflects the actual final size/pos
                 self.parent_window.update_crop_list()

            # Reset state
            self._mode = InteractionMode.NONE
            self._interaction_crop_info = None
            self._active_handle = None
            self._start_interaction_rect = QRectF()
            self.update_hover_cursor(event.pos())
            event.accept(); return

        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        # ... (Zoom logic remains the same) ...
        if not self.get_image_item(): super().wheelEvent(event); return
        zoom_factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        current_zoom = self.transform().m11()
        if (current_zoom * zoom_factor < 0.05 or current_zoom * zoom_factor > 100): return
        self.scale(zoom_factor, zoom_factor)
        event.accept()

    # --- NEW: Key Press Event ---
    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Delete and self._selected_crop_info:
            self.parent_window.delete_selected_crop() # Call main window's delete method
            event.accept()
        else:
            super().keyPressEvent(event) # Pass other keys up

    # --- Cursor and Calculation Helpers (Resize Calculation Corrected) ---
    def update_hover_cursor(self, view_pos: QPointF):
        # ... (Logic mostly unchanged, check handle first, then body, then pan modifier) ...
        if self._mode != InteractionMode.NONE: return
        item = self.itemAt(view_pos)
        if isinstance(item, ResizeHandleItem) and item in self._handle_items:
            self.setCursor(self.get_resize_cursor(item.position)); return
        scene_pos = self.mapToScene(view_pos)
        items_at_scene_pos = self.scene().items(scene_pos)
        # Check topmost visible rect item that isn't a handle
        crop_item_under_cursor = next((i for i in items_at_scene_pos if isinstance(i, QGraphicsRectItem) and i not in self._handle_items and i != self._current_temp_rect_item), None)
        if crop_item_under_cursor:
             self.setCursor(Qt.CursorShape.SizeAllCursor); return
        image_item = self.get_image_item()
        if image_item and image_item.sceneBoundingRect().contains(scene_pos):
            if QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier: self.setCursor(Qt.CursorShape.OpenHandCursor)
            else: self.setCursor(Qt.CursorShape.CrossCursor)
            return
        self.setCursor(Qt.CursorShape.ArrowCursor)


    def get_resize_cursor(self, handle_pos: HandlePosition) -> QCursor:
        # ... (Unchanged) ...
        if handle_pos in (HandlePosition.TOP_LEFT, HandlePosition.BOTTOM_RIGHT): return QCursor(Qt.CursorShape.SizeFDiagCursor)
        elif handle_pos in (HandlePosition.TOP_RIGHT, HandlePosition.BOTTOM_LEFT): return QCursor(Qt.CursorShape.SizeBDiagCursor)
        else: return QCursor(Qt.CursorShape.ArrowCursor)

    # --- CORRECTED Resize Calculation ---
    def calculate_resized_rect(self, original_rect_scene: QRectF, start_scene_pos: QPointF, current_scene_pos: QPointF, handle_pos: HandlePosition, clamp_rect: QRectF) -> QRectF:
        """Calculates the new rectangle geometry during resize, clamped."""
        min_size = 1.0 # Minimum size in scene coords
        new_rect = QRectF(original_rect_scene) # Start with the original rect at the beginning of the drag

        # Determine the fixed point based on the handle being dragged
        fixed_point = QPointF()
        if handle_pos == HandlePosition.TOP_LEFT: fixed_point = original_rect_scene.bottomRight()
        elif handle_pos == HandlePosition.TOP_RIGHT: fixed_point = original_rect_scene.bottomLeft()
        elif handle_pos == HandlePosition.BOTTOM_LEFT: fixed_point = original_rect_scene.topRight()
        elif handle_pos == HandlePosition.BOTTOM_RIGHT: fixed_point = original_rect_scene.topLeft()
        else: return original_rect_scene # Should not happen

        # Calculate the new position of the corner being dragged
        dragged_point = QPointF()
        if handle_pos == HandlePosition.TOP_LEFT: dragged_point = original_rect_scene.topLeft() + (current_scene_pos - start_scene_pos)
        elif handle_pos == HandlePosition.TOP_RIGHT: dragged_point = original_rect_scene.topRight() + (current_scene_pos - start_scene_pos)
        elif handle_pos == HandlePosition.BOTTOM_LEFT: dragged_point = original_rect_scene.bottomLeft() + (current_scene_pos - start_scene_pos)
        elif handle_pos == HandlePosition.BOTTOM_RIGHT: dragged_point = original_rect_scene.bottomRight() + (current_scene_pos - start_scene_pos)

        # Create the ideal new rectangle based on fixed and dragged points
        ideal_rect = QRectF(fixed_point, dragged_point).normalized()

        # Enforce minimum size (expand from fixed point if needed)
        final_rect = QRectF(ideal_rect)
        if final_rect.width() < min_size:
            if handle_pos in (HandlePosition.TOP_LEFT, HandlePosition.BOTTOM_LEFT): final_rect.setLeft(final_rect.right() - min_size)
            else: final_rect.setRight(final_rect.left() + min_size)
        if final_rect.height() < min_size:
             if handle_pos in (HandlePosition.TOP_LEFT, HandlePosition.TOP_RIGHT): final_rect.setTop(final_rect.bottom() - min_size)
             else: final_rect.setBottom(final_rect.top() + min_size)

        # Clamp the final rectangle to the image bounds (clamp_rect)
        # Intersect guarantees it stays within bounds
        clamped_rect = final_rect.intersected(clamp_rect)

        # Final minimum size check after clamping (important if clamping reduces size below min)
        if clamped_rect.width() < min_size: clamped_rect.setWidth(min_size)
        if clamped_rect.height() < min_size: clamped_rect.setHeight(min_size)
        # Re-clamp position after potential min size adjustment near edge
        if clamped_rect.right() > clamp_rect.right(): clamped_rect.moveRight(clamp_rect.right())
        if clamped_rect.bottom() > clamp_rect.bottom(): clamped_rect.moveBottom(clamp_rect.bottom())
        if clamped_rect.left() < clamp_rect.left(): clamped_rect.moveLeft(clamp_rect.left())
        if clamped_rect.top() < clamp_rect.top(): clamped_rect.moveTop(clamp_rect.top())


        return clamped_rect


# --- Main Application Window (UI Enhancements) ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python Multi-Crop Tool")
        self.setGeometry(100, 100, 1200, 700)

        # Data
        self.pil_image: Image.Image | None = None
        self.image_path: str | None = None
        self.crops: list[CropInfo] = []

        # --- UI Setup ---
        self.central_widget = QWidget()
        self.main_layout = QHBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(5, 5, 5, 5) # Reduce margins slightly
        self.setCentralWidget(self.central_widget)

        # Splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_layout.addWidget(self.splitter)

        # Graphics View (Left Side)
        self.scene = QGraphicsScene(self)
        self.view = CropGraphicsView(self.scene, self)
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding) # Allow expanding
        self.view.setMinimumWidth(500) # Reasonable minimum width
        self.splitter.addWidget(self.view)

        # Right Panel (Controls and List)
        self.right_panel = QWidget()
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setContentsMargins(10, 10, 10, 10) # Add padding inside panel
        self.right_layout.setSpacing(10) # Spacing between widgets
        self.right_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.right_panel.setMinimumWidth(220)
        self.right_panel.setMaximumWidth(400)
        self.splitter.addWidget(self.right_panel)

        # Crop List Label
        self.crop_list_label = QLabel("Defined Crops:")
        self.right_layout.addWidget(self.crop_list_label)

        # Crop List
        self.crop_list_widget = QListWidget()
        self.crop_list_widget.setAlternatingRowColors(True) # Improve readability
        self.crop_list_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding) # Let list expand vertically
        self.right_layout.addWidget(self.crop_list_widget)

        # Buttons Layout
        self.button_layout = QHBoxLayout()
        self.delete_button = QPushButton("Delete Selected")
        self.delete_button.setToolTip("Delete the crop selected in the list or view (Del key)")
        self.download_button = QPushButton("Download Crops (ZIP)")
        self.download_button.setToolTip("Save all defined crops into a ZIP file")
        self.button_layout.addWidget(self.delete_button)
        self.button_layout.addSpacing(10) # Space between buttons
        self.button_layout.addWidget(self.download_button)
        self.right_layout.addLayout(self.button_layout)

        # Set initial splitter sizes
        self.splitter.setSizes([800, 250])

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready. Open an image using File > Open.")

        # Create Actions and Menu
        self._create_actions()
        self._create_menu()

        # Connect Signals
        self.crop_list_widget.itemSelectionChanged.connect(self.on_crop_selection_changed)
        self.view.crop_selected_signal.connect(self.on_view_selection_changed)
        self.view.crops_updated_signal.connect(self.update_crop_list)
        self.view.status_message_signal.connect(self.status_bar.showMessage) # Connect view status signal
        self.delete_button.clicked.connect(self.delete_selected_crop)
        self.download_button.clicked.connect(self.download_crops)

    # --- Actions and Menu (Unchanged) ---
    def _create_actions(self):
        self.open_action = QAction("&Open Image...", self)
        self.open_action.triggered.connect(self.open_image)
        self.exit_action = QAction("E&xit", self)
        self.exit_action.triggered.connect(self.close)

    def _create_menu(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")
        file_menu.addAction(self.open_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

    # --- Core Logic Methods (Minor changes for status bar) ---
    def open_image(self):
        file_path, _ = QFileDialog.getOpenFileName(self,"Open Image","", "Image Files (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All Files (*)")
        if file_path:
            try:
                self.pil_image = Image.open(file_path)
                self.image_path = file_path
                self.scene.clear(); self.crops.clear(); CropInfo._next_id = 1
                self.view.set_selected_crop(None)
                q_image = pillow_to_qimage(self.pil_image); pixmap = QPixmap.fromImage(q_image)
                image_item = self.scene.addPixmap(pixmap); image_item.setZValue(-1)
                self.view.resetTransform()
                self.view.fitInView(image_item, Qt.AspectRatioMode.KeepAspectRatio)
                self.update_crop_list()
                self.status_bar.showMessage(f"Loaded: {os.path.basename(file_path)} ({self.pil_image.width}x{self.pil_image.height})", 5000) # Show for 5 secs
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open image:\n{e}")
                self.pil_image = None; self.image_path = None; self.scene.clear(); self.crops.clear()
                self.update_crop_list()
                self.status_bar.showMessage("Failed to load image.")

    def update_crop_list(self):
        selected_info = self.view._selected_crop_info
        self.crop_list_widget.blockSignals(True)
        self.crop_list_widget.clear()
        list_item_map = {}
        for crop_info in sorted(self.crops, key=lambda c: c.id):
            list_item = QListWidgetItem(str(crop_info))
            list_item.setData(Qt.ItemDataRole.UserRole, crop_info)
            self.crop_list_widget.addItem(list_item)
            list_item_map[crop_info] = list_item
            if crop_info == selected_info: list_item.setSelected(True)
        self.crop_list_widget.blockSignals(False)
        # Update button states
        has_selection = selected_info is not None
        has_crops = len(self.crops) > 0
        self.delete_button.setEnabled(has_selection)
        self.download_button.setEnabled(has_crops)


    def on_crop_selection_changed(self):
        # ... (Unchanged) ...
        selected_items = self.crop_list_widget.selectedItems()
        if selected_items:
            list_item = selected_items[0]
            crop_info = list_item.data(Qt.ItemDataRole.UserRole)
            if isinstance(crop_info, CropInfo): self.view.set_selected_crop(crop_info)
        else: self.view.set_selected_crop(None)
        self.delete_button.setEnabled(len(selected_items) > 0) # Update delete button state


    def on_view_selection_changed(self, selected_crop_info: CropInfo | None):
        # ... (Unchanged, updates list selection based on view) ...
        self.crop_list_widget.blockSignals(True)
        found_item = None
        for i in range(self.crop_list_widget.count()):
            item = self.crop_list_widget.item(i)
            item_data = item.data(Qt.ItemDataRole.UserRole)
            if item_data == selected_crop_info: found_item = item; break
        if found_item: self.crop_list_widget.setCurrentItem(found_item)
        else: self.crop_list_widget.clearSelection()
        self.crop_list_widget.blockSignals(False)
        self.delete_button.setEnabled(selected_crop_info is not None) # Update delete button state

    # --- Delete method now called by view's key press or button click ---
    def delete_selected_crop(self):
        selected_info = self.view._selected_crop_info # Get selection from the view
        if selected_info:
            reply = QMessageBox.question(self, "Confirm Delete",
                                         f"Delete Crop {selected_info.id}?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.scene.removeItem(selected_info.rect_item)
                # Handles are children, removed automatically, but clear view's list
                for handle in self.view._handle_items: self.scene().removeItem(handle) # Ensure removal if parenting fails
                self.view._handle_items.clear()
                self.crops.remove(selected_info)
                self.view.set_selected_crop(None)
                self.update_crop_list()
                self.status_bar.showMessage(f"Crop {selected_info.id} deleted.", 3000)


    def download_crops(self):
        # ... (Download logic remains largely the same, add status messages) ...
        if not self.pil_image or not self.crops:
            QMessageBox.information(self, "No Crops", "Please load an image and define crops first.")
            return
        base_filename = "cropped_images"
        if self.image_path: base_filename = os.path.splitext(os.path.basename(self.image_path))[0] + "_crops"
        save_path, _ = QFileDialog.getSaveFileName(self,"Save Crops ZIP",f"{base_filename}.zip","ZIP Files (*.zip)")
        if save_path:
            try:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                self.status_bar.showMessage("Saving crops to ZIP...")
                QApplication.processEvents() # Allow UI to update
                count = 0
                with zipfile.ZipFile(save_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for i, crop_info in enumerate(self.crops):
                        rect_f = crop_info.get_rect_image_coords()
                        left = max(0, int(round(rect_f.left()))); upper = max(0, int(round(rect_f.top())))
                        right = min(self.pil_image.width, int(round(rect_f.right())))
                        lower = min(self.pil_image.height, int(round(rect_f.bottom())))
                        if right > left and lower > upper:
                            box = (left, upper, right, lower)
                            cropped_pil = self.pil_image.crop(box)
                            img_byte_arr = io.BytesIO()
                            cropped_pil.save(img_byte_arr, format='PNG')
                            img_byte_arr.seek(0)
                            zip_filename = f"{base_filename.split('_crops')[0]}_crop_{crop_info.id}.png" # Use original base name
                            zipf.writestr(zip_filename, img_byte_arr.getvalue())
                            count += 1
                self.status_bar.showMessage(f"Successfully saved {count} crops to {os.path.basename(save_path)}", 5000)
                # QMessageBox.information(self, "Success", f"Successfully saved {count} crops to\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save crops:\n{e}")
                self.status_bar.showMessage("Error saving crops.")
            finally:
                 QApplication.restoreOverrideCursor()

# --- Main Execution Block (Unchanged) ---
if __name__ == "__main__":
    app = QApplication(sys.argv)
    # Optional: Apply a simple global style hint
    # app.setStyle("Fusion")
    main_window = MainWindow()
    main_window.show()
    sys.exit(app.exec())
