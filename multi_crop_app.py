import sys
import os
import io
import zipfile
from enum import Enum, auto

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QListWidgetItem, QFileDialog, QMessageBox,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem,
    QSplitter, QMenu, QSizePolicy
)
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QPen, QBrush, QColor, QTransform, QCursor, QAction
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal

from PIL import Image

# Helper function to convert Pillow Image to QImage
def pillow_to_qimage(pil_img):
    """Converts a Pillow image to a QImage."""
    if pil_img.mode == "RGB":
        pass
    elif pil_img.mode == "RGBA":
        pass
    else: # Convert other modes to RGBA for QImage compatibility
        pil_img = pil_img.convert("RGBA")

    # Get image data
    data = pil_img.tobytes("raw", pil_img.mode)

    # Determine QImage format
    if pil_img.mode == "RGB":
        qimage_format = QImage.Format.Format_RGB888
    elif pil_img.mode == "RGBA":
        qimage_format = QImage.Format.Format_RGBA8888
    else:
        # Should not happen after conversion, but fallback
        qimage_format = QImage.Format.Format_RGBA8888

    # Create QImage
    qimage = QImage(data, pil_img.width, pil_img.height, qimage_format)
    # Important: PyQt doesn't take ownership of the data buffer with this constructor.
    # If the Pillow image data goes out of scope, the QImage might become invalid.
    # Creating a copy ensures the data persists.
    return qimage.copy()

# --- Enums and Data Classes ---
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
    # Add TOP, BOTTOM, LEFT, RIGHT later if needed

class CropInfo:
    _next_id = 1
    def __init__(self, rect_item: QGraphicsRectItem):
        self.id = CropInfo._next_id
        CropInfo._next_id += 1
        self.rect_item = rect_item # Holds the QGraphicsRectItem representing the crop

    def get_rect_image_coords(self) -> QRectF:
        """Get the rectangle in image (scene) coordinates."""
        return self.rect_item.rect()

    def set_rect_image_coords(self, rect: QRectF):
        """Set the rectangle in image (scene) coordinates."""
        self.rect_item.setRect(rect)

    def __str__(self):
        rect = self.get_rect_image_coords()
        return f"Crop {self.id}: (W: {rect.width():.0f}, H: {rect.height():.0f})"

# --- Custom Graphics Item for Handles ---
class ResizeHandleItem(QGraphicsRectItem):
    """A small rectangle item representing a resize handle."""
    def __init__(self, parent_crop_item: QGraphicsRectItem, position: HandlePosition, size: float = 8.0):
        super().__init__(-size / 2, -size / 2, size, size, parent=parent_crop_item) # Centered rect
        self.parent_crop_item = parent_crop_item
        self.position = position
        self.handle_size = size

        self.setBrush(QBrush(QColor(0, 123, 255, 220)))
        self.setPen(QPen(QColor(255, 255, 255, 200), 1.0))
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIgnoresTransformations) # Keep size constant on zoom
        self.setZValue(10) # Ensure handles are drawn on top
        self.update_position()

    def update_position(self):
        """Updates the handle's position relative to the parent crop rect."""
        parent_rect = self.parent_crop_item.rect()
        center_x, center_y = 0, 0
        if self.position == HandlePosition.TOP_LEFT:
            center_x, center_y = parent_rect.left(), parent_rect.top()
        elif self.position == HandlePosition.TOP_RIGHT:
            center_x, center_y = parent_rect.right(), parent_rect.top()
        elif self.position == HandlePosition.BOTTOM_LEFT:
            center_x, center_y = parent_rect.left(), parent_rect.bottom()
        elif self.position == HandlePosition.BOTTOM_RIGHT:
            center_x, center_y = parent_rect.right(), parent_rect.bottom()
        # Add other positions if needed

        self.setPos(center_x, center_y) # Set position relative to parent

# --- Custom Graphics View ---
class CropGraphicsView(QGraphicsView):
    # Define signals if needed for communication with the main window
    crop_selected_signal = pyqtSignal(object) # Pass CropInfo or None
    crops_updated_signal = pyqtSignal()

    def __init__(self, scene: QGraphicsScene, parent=None):
        super().__init__(scene, parent)
        self.parent_window = parent # Reference to the main window
        self._mode = InteractionMode.NONE
        self._start_pan_pos = QPointF()
        self._start_scene_pos = QPointF() # Mouse pos in scene coords for draw/move/resize
        self._current_temp_rect_item: QGraphicsRectItem | None = None
        self._selected_crop_info: CropInfo | None = None
        self._interaction_crop_info: CropInfo | None = None # Crop being moved/resized
        self._active_handle: ResizeHandleItem | None = None
        self._handle_items: list[ResizeHandleItem] = []

        # Pens and brushes
        self.crop_pen = QPen(QColor(255, 0, 0, 180), 1.5)
        self.crop_pen.setCosmetic(True) # Keep width constant regardless of zoom
        self.selected_crop_pen = QPen(QColor(0, 123, 255, 200), 2.5)
        self.selected_crop_pen.setCosmetic(True)
        self.temp_draw_pen = QPen(QColor(0, 123, 255, 200), 2.0, Qt.PenStyle.DashLine)
        self.temp_draw_pen.setCosmetic(True)

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.NoDrag) # We handle dragging manually
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True) # Receive mouse move events even when no button is pressed

    def get_image_item(self) -> QGraphicsPixmapItem | None:
        items = self.scene().items()
        for item in items:
            if isinstance(item, QGraphicsPixmapItem):
                return item
        return None

    def set_selected_crop(self, crop_info: CropInfo | None):
        """Sets the currently selected crop and updates visuals."""
        if self._selected_crop_info == crop_info:
            return # No change

        # Deselect previous
        if self._selected_crop_info:
            self._selected_crop_info.rect_item.setPen(self.crop_pen)
            # Remove old handles
            for handle in self._handle_items:
                self.scene().removeItem(handle)
            self._handle_items.clear()

        self._selected_crop_info = crop_info

        # Select new
        if self._selected_crop_info:
            self._selected_crop_info.rect_item.setPen(self.selected_crop_pen)
            self._selected_crop_info.rect_item.setZValue(1) # Bring selected slightly forward
            # Add new handles
            parent_rect_item = self._selected_crop_info.rect_item
            for pos_enum in [HandlePosition.TOP_LEFT, HandlePosition.TOP_RIGHT, HandlePosition.BOTTOM_LEFT, HandlePosition.BOTTOM_RIGHT]:
                 handle = ResizeHandleItem(parent_rect_item, pos_enum)
                 self._handle_items.append(handle)
                 # Note: Handle is added to scene automatically as it's parented
            self.update_handle_positions() # Initial placement
        else:
            # If deselecting, ensure other items have normal ZValue
             for item in self.scene().items():
                 if isinstance(item, QGraphicsRectItem) and item != self._current_temp_rect_item:
                     item.setZValue(0)

        self.crop_selected_signal.emit(self._selected_crop_info) # Notify main window
        self.viewport().update() # Request redraw

    def update_handle_positions(self):
        """Call this after the selected crop's rectangle geometry changes."""
        for handle in self._handle_items:
            handle.update_position()

    # --- Mouse Events ---
    def mousePressEvent(self, event):
        image_item = self.get_image_item()
        if not image_item:
            super().mousePressEvent(event)
            return

        self._start_scene_pos = self.mapToScene(event.pos())

        # 1. Panning Start
        if event.button() == Qt.MouseButton.MiddleButton or \
           (event.button() == Qt.MouseButton.LeftButton and QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier):
            self._mode = InteractionMode.PANNING
            self._start_pan_pos = event.pos() # Use viewport coords for panning delta
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            # Check for clicks on handles of the selected item first
            if self._selected_crop_info:
                clicked_item = self.itemAt(event.pos())
                if isinstance(clicked_item, ResizeHandleItem) and clicked_item in self._handle_items:
                    self._mode = InteractionMode.RESIZING
                    self._interaction_crop_info = self._selected_crop_info
                    self._active_handle = clicked_item
                    # Use scene position as reference for resize delta
                    self._start_scene_pos = self.mapToScene(event.pos())
                    self.setCursor(self.get_resize_cursor(clicked_item.position))
                    event.accept()
                    return

            # Check for clicks on item bodies (selected or otherwise)
            clicked_item = self.itemAt(event.pos()) # Re-check item at pos
            if isinstance(clicked_item, QGraphicsRectItem) and clicked_item not in self._handle_items and clicked_item != self._current_temp_rect_item:
                # Find corresponding CropInfo
                found_crop = None
                for info in self.parent_window.crops: # Access main window's list
                    if info.rect_item == clicked_item:
                        found_crop = info
                        break

                if found_crop:
                    self._mode = InteractionMode.MOVING
                    self.set_selected_crop(found_crop) # Select the clicked crop
                    self._interaction_crop_info = found_crop
                     # Use scene position as reference for move delta
                    self._start_scene_pos = self.mapToScene(event.pos())
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                    event.accept()
                    return

            # If clicked empty area (or image itself), start drawing
            # Ensure click is within image bounds in scene coords
            img_bounds = image_item.sceneBoundingRect()
            if img_bounds.contains(self._start_scene_pos):
                self._mode = InteractionMode.DRAWING
                self.set_selected_crop(None) # Deselect any crop
                # Create temporary rectangle for visual feedback
                if self._current_temp_rect_item:
                    self.scene().removeItem(self._current_temp_rect_item)
                self._current_temp_rect_item = QGraphicsRectItem(QRectF(self._start_scene_pos, QPointF(0,0)))
                self._current_temp_rect_item.setPen(self.temp_draw_pen)
                self.scene().addItem(self._current_temp_rect_item)
                self.setCursor(Qt.CursorShape.CrossCursor)
                event.accept()
                return

        super().mousePressEvent(event) # Pass event up if not handled


    def mouseMoveEvent(self, event):
        current_scene_pos = self.mapToScene(event.pos())
        image_item = self.get_image_item()
        if not image_item:
            super().mouseMoveEvent(event)
            return

        img_rect_scene = image_item.sceneBoundingRect() # Image bounds in scene coords

        if self._mode == InteractionMode.PANNING:
            delta = event.pos() - self._start_pan_pos
            # Translate the view (inverted delta)
            self.translate(delta.x(), delta.y())
            self._start_pan_pos = event.pos()
            event.accept()
            return

        if self._mode == InteractionMode.DRAWING and self._current_temp_rect_item:
            # Clamp drawing rect to image bounds
            rect_sc = QRectF(self._start_scene_pos, current_scene_pos).normalized()
            clamped_rect = rect_sc.intersected(img_rect_scene)
            self._current_temp_rect_item.setRect(clamped_rect)
            event.accept()
            return

        if self._mode == InteractionMode.MOVING and self._interaction_crop_info:
            delta = current_scene_pos - self._start_scene_pos
            original_rect = self._interaction_crop_info.rect_item.rect()
            new_top_left = original_rect.topLeft() + delta

            # Clamp movement within image bounds
            if new_top_left.x() < img_rect_scene.left(): new_top_left.setX(img_rect_scene.left())
            if new_top_left.y() < img_rect_scene.top(): new_top_left.setY(img_rect_scene.top())
            if new_top_left.x() + original_rect.width() > img_rect_scene.right():
                new_top_left.setX(img_rect_scene.right() - original_rect.width())
            if new_top_left.y() + original_rect.height() > img_rect_scene.bottom():
                 new_top_left.setY(img_rect_scene.bottom() - original_rect.height())

            self._interaction_crop_info.rect_item.setPos(new_top_left) # Move the item group
            # Since handles are children, they move with the item. Update their internal position logic if needed.
            # self.update_handle_positions() # Not strictly necessary if using setPos
            self._start_scene_pos = current_scene_pos # Update reference point
            self.crops_updated_signal.emit() # Notify list might need update
            event.accept()
            return

        if self._mode == InteractionMode.RESIZING and self._interaction_crop_info and self._active_handle:
            original_rect_parent_coords = self._interaction_crop_info.rect_item.rect() # Rect relative to item's pos (0,0)
            parent_pos = self._interaction_crop_info.rect_item.pos() # Item's top-left pos in scene
            original_rect_scene = QRectF(parent_pos, original_rect_parent_coords.size()) # Original rect in scene coords

            # Calculate new rect based on handle and mouse pos (clamp to image)
            new_rect_scene = self.calculate_resized_rect(
                original_rect_scene,
                self._start_scene_pos,
                current_scene_pos,
                self._active_handle.position,
                img_rect_scene # Clamp boundary
            )

            # Update the QGraphicsRectItem
            # Set new position (top-left) and new rectangle size relative to that position
            self._interaction_crop_info.rect_item.setPos(new_rect_scene.topLeft())
            self._interaction_crop_info.rect_item.setRect(QRectF(QPointF(0,0), new_rect_scene.size())) # Rect relative to top-left

            self.update_handle_positions() # Update handle positions based on new rect
            self._start_scene_pos = current_scene_pos # Update reference point
            self.crops_updated_signal.emit() # Notify list might need update
            event.accept()
            return

        # If not interacting, update cursor based on hover
        if self._mode == InteractionMode.NONE:
            self.update_hover_cursor(event.pos())

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._mode == InteractionMode.NONE:
            super().mouseReleaseEvent(event)
            return

        if event.button() == Qt.MouseButton.MiddleButton and self._mode == InteractionMode.PANNING:
            self.setCursor(Qt.CursorShape.ArrowCursor) # Or update hover cursor
            self._mode = InteractionMode.NONE
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            if self._mode == InteractionMode.DRAWING and self._current_temp_rect_item:
                final_rect_scene = self._current_temp_rect_item.rect()
                self.scene().removeItem(self._current_temp_rect_item)
                self._current_temp_rect_item = None

                min_size = 5 # Minimum pixel size (in scene/image coords)
                if final_rect_scene.width() > min_size and final_rect_scene.height() > min_size:
                    # Create permanent crop item
                    crop_item = QGraphicsRectItem(final_rect_scene)
                    crop_item.setPen(self.crop_pen)
                    self.scene().addItem(crop_item)

                    new_crop_info = CropInfo(crop_item)
                    self.parent_window.crops.append(new_crop_info) # Add to main list
                    self.parent_window.update_crop_list() # Update UI list
                    self.set_selected_crop(new_crop_info) # Select the new crop
                else:
                    self.set_selected_crop(None) # Didn't create a valid crop

            elif self._mode == InteractionMode.MOVING or self._mode == InteractionMode.RESIZING:
                 # Final position/size is already set during move, just update UI list representation
                 self.parent_window.update_crop_list() # Ensure text is correct

            # Reset state
            self._mode = InteractionMode.NONE
            self._interaction_crop_info = None
            self._active_handle = None
            self.update_hover_cursor(event.pos()) # Set correct cursor for release position
            event.accept()
            return

        super().mouseReleaseEvent(event)


    def wheelEvent(self, event):
        if not self.get_image_item():
            super().wheelEvent(event)
            return

        # Zoom Factor
        zoom_factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15

        # Limit zoom
        current_zoom = self.transform().m11() # Assuming no shear/rotation
        if (current_zoom * zoom_factor < 0.05 or # Min zoom limit
            current_zoom * zoom_factor > 100):    # Max zoom limit
             return

        self.scale(zoom_factor, zoom_factor)
        event.accept()

    def update_hover_cursor(self, view_pos: QPointF):
        """Sets the cursor based on what's under the mouse."""
        if self._mode != InteractionMode.NONE: return # Don't change during interaction

        # Check handles first
        item = self.itemAt(view_pos)
        if isinstance(item, ResizeHandleItem) and item in self._handle_items:
            self.setCursor(self.get_resize_cursor(item.position))
            return

        # Check crop bodies
        if isinstance(item, QGraphicsRectItem) and item not in self._handle_items and item != self._current_temp_rect_item:
            self.setCursor(Qt.CursorShape.SizeAllCursor) # Indicate movable
            return

        # Check if over image (for drawing)
        image_item = self.get_image_item()
        if image_item and image_item.contains(self.mapToScene(view_pos)):
             # Check for panning modifier
            if QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
            return

        # Default cursor
        self.setCursor(Qt.CursorShape.ArrowCursor)


    def get_resize_cursor(self, handle_pos: HandlePosition) -> QCursor:
        """Returns the appropriate resize cursor for a handle position."""
        if handle_pos in (HandlePosition.TOP_LEFT, HandlePosition.BOTTOM_RIGHT):
            return QCursor(Qt.CursorShape.SizeFDiagCursor)
        elif handle_pos in (HandlePosition.TOP_RIGHT, HandlePosition.BOTTOM_LEFT):
            return QCursor(Qt.CursorShape.SizeBDiagCursor)
        # Add SizeVerCursor and SizeHorCursor if using side handles
        else:
            return QCursor(Qt.CursorShape.ArrowCursor)


    def calculate_resized_rect(self, original_rect_scene: QRectF, start_scene_pos: QPointF, current_scene_pos: QPointF, handle_pos: HandlePosition, clamp_rect: QRectF) -> QRectF:
        """Calculates the new rectangle geometry during resize, clamped."""
        min_size = 1.0 # Minimum size in scene coords

        new_rect = QRectF(original_rect_scene) # Start with a copy
        delta = current_scene_pos - start_scene_pos

        # Adjust edges based on handle
        if handle_pos == HandlePosition.TOP_LEFT:
            new_rect.setTopLeft(original_rect_scene.topLeft() + delta)
        elif handle_pos == HandlePosition.TOP_RIGHT:
            new_rect.setTopRight(original_rect_scene.topRight() + delta)
        elif handle_pos == HandlePosition.BOTTOM_LEFT:
            new_rect.setBottomLeft(original_rect_scene.bottomLeft() + delta)
        elif handle_pos == HandlePosition.BOTTOM_RIGHT:
            new_rect.setBottomRight(original_rect_scene.bottomRight() + delta)
        # Add other handles

        # Ensure minimum size (adjusting opposite corner/edge)
        if new_rect.width() < min_size:
            if handle_pos in (HandlePosition.TOP_LEFT, HandlePosition.BOTTOM_LEFT):
                new_rect.setLeft(new_rect.right() - min_size)
            else:
                new_rect.setRight(new_rect.left() + min_size)
        if new_rect.height() < min_size:
             if handle_pos in (HandlePosition.TOP_LEFT, HandlePosition.TOP_RIGHT):
                new_rect.setTop(new_rect.bottom() - min_size)
             else:
                new_rect.setBottom(new_rect.top() + min_size)

        # Clamp the final rectangle to the image bounds
        return new_rect.intersected(clamp_rect)


# --- Main Application Window ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python Multi-Crop Tool")
        self.setGeometry(100, 100, 1200, 700) # x, y, width, height

        # Data
        self.pil_image: Image.Image | None = None
        self.image_path: str | None = None
        self.crops: list[CropInfo] = [] # List of CropInfo objects

        # UI Elements
        self.central_widget = QWidget()
        self.main_layout = QHBoxLayout(self.central_widget)
        self.setCentralWidget(self.central_widget)

        # Splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_layout.addWidget(self.splitter)

        # Graphics View (Left Side)
        self.scene = QGraphicsScene(self)
        self.view = CropGraphicsView(self.scene, self)
        self.view.setMinimumWidth(400)
        self.splitter.addWidget(self.view)

        # Right Panel (Controls and List)
        self.right_panel = QWidget()
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_panel.setMinimumWidth(200)
        self.right_panel.setMaximumWidth(350)
        self.splitter.addWidget(self.right_panel)

        # Crop List
        self.crop_list_widget = QListWidget()
        self.right_layout.addWidget(self.crop_list_widget)

        # Buttons
        self.button_layout = QHBoxLayout()
        self.delete_button = QPushButton("Delete Selected")
        self.download_button = QPushButton("Download Crops (ZIP)")
        self.button_layout.addWidget(self.delete_button)
        self.button_layout.addWidget(self.download_button)
        self.right_layout.addLayout(self.button_layout)

        # Set initial splitter sizes
        self.splitter.setSizes([800, 250]) # Initial widths for left and right

        # Create Actions and Menu
        self._create_actions()
        self._create_menu()

        # Connect Signals
        self.crop_list_widget.itemSelectionChanged.connect(self.on_crop_selection_changed)
        self.view.crop_selected_signal.connect(self.on_view_selection_changed) # Connect view signal
        self.view.crops_updated_signal.connect(self.update_crop_list) # Update list when crops modified
        self.delete_button.clicked.connect(self.delete_selected_crop)
        self.download_button.clicked.connect(self.download_crops)

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

    def open_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Image",
            "", # Start directory
            "Image Files (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All Files (*)"
        )
        if file_path:
            try:
                # Load image with Pillow
                self.pil_image = Image.open(file_path)
                self.image_path = file_path

                # Clear previous state
                self.scene.clear()
                self.crops.clear()
                CropInfo._next_id = 1 # Reset ID counter
                self.view.set_selected_crop(None) # Clear selection in view

                # Convert Pillow image to QPixmap and add to scene
                q_image = pillow_to_qimage(self.pil_image)
                pixmap = QPixmap.fromImage(q_image)
                image_item = self.scene.addPixmap(pixmap)
                image_item.setZValue(-1) # Ensure image is behind crops

                # Reset view transform
                self.view.resetTransform()
                # Optional: Fit image initially
                self.view.fitInView(image_item, Qt.AspectRatioMode.KeepAspectRatio)

                self.update_crop_list() # Clear listbox

            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open image:\n{e}")
                self.pil_image = None
                self.image_path = None
                self.scene.clear()
                self.crops.clear()
                self.update_crop_list()

    def update_crop_list(self):
        """Updates the QListWidget based on the self.crops list."""
        selected_info = self.view._selected_crop_info # Get current selection from view

        self.crop_list_widget.blockSignals(True) # Prevent selection signals during update
        self.crop_list_widget.clear()

        list_item_map = {} # Map CropInfo to QListWidgetItem

        for crop_info in sorted(self.crops, key=lambda c: c.id):
            list_item = QListWidgetItem(str(crop_info))
            list_item.setData(Qt.ItemDataRole.UserRole, crop_info) # Store CropInfo object
            self.crop_list_widget.addItem(list_item)
            list_item_map[crop_info] = list_item

            if crop_info == selected_info:
                list_item.setSelected(True) # Reselect item in list

        self.crop_list_widget.blockSignals(False)


    def on_crop_selection_changed(self):
        """Handles selection changes in the QListWidget."""
        selected_items = self.crop_list_widget.selectedItems()
        if selected_items:
            list_item = selected_items[0]
            crop_info = list_item.data(Qt.ItemDataRole.UserRole)
            if isinstance(crop_info, CropInfo):
                self.view.set_selected_crop(crop_info) # Tell the view to select this crop
        else:
            self.view.set_selected_crop(None) # No selection in list -> deselect in view

    def on_view_selection_changed(self, selected_crop_info: CropInfo | None):
        """Handles selection changes originating from the QGraphicsView."""
        self.crop_list_widget.blockSignals(True)
        found_item = None
        for i in range(self.crop_list_widget.count()):
            item = self.crop_list_widget.item(i)
            item_data = item.data(Qt.ItemDataRole.UserRole)
            if item_data == selected_crop_info:
                found_item = item
                break

        if found_item:
             self.crop_list_widget.setCurrentItem(found_item) # Select corresponding item in list
        else:
            self.crop_list_widget.clearSelection() # Deselect list if view deselects

        self.crop_list_widget.blockSignals(False)

    def delete_selected_crop(self):
        selected_info = self.view._selected_crop_info
        if selected_info:
            # Remove graphics item from scene
            self.scene.removeItem(selected_info.rect_item)
            # Remove handles associated with it
            for handle in self.view._handle_items: # Access view's handle list
                 self.scene.removeItem(handle)
            self.view._handle_items.clear()

            # Remove from data list
            self.crops.remove(selected_info)

            # Clear selection and update UI
            self.view.set_selected_crop(None)
            self.update_crop_list()

    def download_crops(self):
        if not self.pil_image or not self.crops:
            QMessageBox.information(self, "No Crops", "Please load an image and define crops first.")
            return

        # Get base filename
        base_filename = "cropped_images"
        if self.image_path:
            base_filename = os.path.splitext(os.path.basename(self.image_path))[0] + "_crops"

        # Ask user where to save the ZIP file
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Crops ZIP",
            f"{base_filename}.zip",
            "ZIP Files (*.zip)"
        )

        if save_path:
            try:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor) # Busy cursor
                with zipfile.ZipFile(save_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for i, crop_info in enumerate(self.crops):
                        # Get rect in image coordinates (same as scene coords here)
                        rect_f = crop_info.get_rect_image_coords()

                        # Convert QRectF to tuple for Pillow (left, upper, right, lower)
                        # Ensure coordinates are within image bounds and integers
                        left = max(0, int(round(rect_f.left())))
                        upper = max(0, int(round(rect_f.top())))
                        right = min(self.pil_image.width, int(round(rect_f.right())))
                        lower = min(self.pil_image.height, int(round(rect_f.bottom())))

                        # Check for valid size after clamping
                        if right > left and lower > upper:
                            box = (left, upper, right, lower)
                            cropped_pil = self.pil_image.crop(box)

                            # Save cropped image to buffer
                            img_byte_arr = io.BytesIO()
                            cropped_pil.save(img_byte_arr, format='PNG') # Save as PNG
                            img_byte_arr.seek(0) # Rewind buffer

                            # Add buffer to zip file
                            zip_filename = f"{base_filename}_crop_{crop_info.id}.png"
                            zipf.writestr(zip_filename, img_byte_arr.getvalue())

                QMessageBox.information(self, "Success", f"Successfully saved {len(self.crops)} crops to\n{save_path}")

            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save crops:\n{e}")
            finally:
                 QApplication.restoreOverrideCursor() # Restore cursor


if __name__ == "__main__":
    app = QApplication(sys.argv)
    main_window = MainWindow()
    main_window.show()
    sys.exit(app.exec())
