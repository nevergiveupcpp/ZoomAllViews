"""
ZoomAllViews — Ctrl+Scroll font zoom for every IDA view.

Compatibility : IDA 8.x — 9.3+ (PyQt5 / PySide6)
Installation  : Copy to <IDA_DIR>/plugins/
Usage         : Ctrl + Scroll Up/Down
Toggle        : Edit -> Plugins -> ZoomAllViews  |  Ctrl-Shift-Z
"""

__author__ = "Jiri Vinopal (@Dump-GUY)"
__version__ = "1.0.2"

import time
from dataclasses import dataclass

import ida_idaapi
import ida_kernwin

# -----------------------------------------------------------------------
# Qt compatibility layer — PyQt5 (IDA ≤ 9.1) / PySide6 (IDA ≥ 9.2)
# -----------------------------------------------------------------------

try:
    from PySide6.QtWidgets import (QApplication, QWidget, QAbstractScrollArea,
                                    QTableView, QTreeView)
    from PySide6.QtCore import QObject, QEvent, Qt, QTimer, QRect
    from PySide6.QtGui import QFontMetrics, QPainter, QColor, QFont, QPalette
    from shiboken6 import getCppPointer, isValid
    QT_MODULE = "pyside6"

    WHEEL_EVENT = QEvent.Type.Wheel
    CTRL_MODIFIER = Qt.KeyboardModifier.ControlModifier
    WA_TRANSPARENT_FOR_MOUSE = Qt.WidgetAttribute.WA_TransparentForMouseEvents
    WA_TRANSLUCENT_BACKGROUND = Qt.WidgetAttribute.WA_TranslucentBackground
    ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter
    NO_PEN = Qt.PenStyle.NoPen
    ANTIALIASING = QPainter.RenderHint.Antialiasing
    OVERLAY_BASE = QPalette.ColorRole.Base
    OVERLAY_TEXT = QPalette.ColorRole.Text

    def _qt_is_valid(widget):
        return isValid(widget)

    def _qt_pointer(widget):
        return int(getCppPointer(widget)[0])

    def _global_pos(event):
        return event.globalPosition().toPoint()

except ImportError:
    from PyQt5.QtWidgets import (QApplication, QWidget, QAbstractScrollArea,
                                  QTableView, QTreeView)
    from PyQt5.QtCore import QObject, QEvent, Qt, QTimer, QRect
    from PyQt5.QtGui import QFontMetrics, QPainter, QColor, QFont, QPalette
    try:
        from PyQt5 import sip
    except ImportError:
        import sip
    QT_MODULE = "pyqt5"

    WHEEL_EVENT = QEvent.Wheel
    CTRL_MODIFIER = Qt.ControlModifier
    WA_TRANSPARENT_FOR_MOUSE = Qt.WA_TransparentForMouseEvents
    WA_TRANSLUCENT_BACKGROUND = Qt.WA_TranslucentBackground
    ALIGN_CENTER = Qt.AlignCenter
    NO_PEN = Qt.NoPen
    ANTIALIASING = QPainter.Antialiasing
    OVERLAY_BASE = QPalette.Base
    OVERLAY_TEXT = QPalette.Text

    def _qt_is_valid(widget):
        return not sip.isdeleted(widget)


    def _qt_pointer(widget):
        try:
            return int(sip.unwrapinstance(widget))
        except Exception:
            return id(widget)


    def _global_pos(event):
        return event.globalPos()


# -----------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------

SIZE_PROP = "_zav_size"
PLUGIN_NAME = "ZoomAllViews"
PLUGIN_HOTKEY = "Ctrl-Shift-Z"


@dataclass(frozen=True)
class ZoomConfig:
    min_font_size: int = 6
    max_font_size: int = 40
    default_font_size: int = 10
    step: int = 1
    row_extra_height: int = 6


@dataclass(frozen=True)
class OverlayConfig:
    hold_seconds: float = 1.0
    fade_seconds: float = 0.85
    tick_ms: int = 33

    reference_width: int = 1920
    reference_height: int = 1080

    width: int = 100
    height: int = 44
    font_size: int = 13
    font_bold: bool = True
    top_padding: int = 15
    right_padding: int = 15
    radius: int = 8

    background_alpha: int = 220
    border_alpha: int = 70


zoom_config = ZoomConfig()
overlay_config = OverlayConfig()


# -----------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------

def _find_scroll_area(widget):
    """Walk up to the nearest QAbstractScrollArea parent."""
    w = widget
    while w is not None:
        if isinstance(w, QAbstractScrollArea):
            return w
        p = w.parent()
        if not isinstance(p, QWidget):
            return widget
        w = p
    return widget


def _is_graph_view(target, container):
    """Detect graph-based views that handle their own Ctrl+Scroll zoom:
    - IDA View in graph mode (TCCRT_GRAPH)
    - IDA View in proximity mode (TCCRT_PROXIMITY)
    - Xref/call graph windows (QOpenGLWidget renderer)
    """
    try:
        twidget = ida_kernwin.get_current_widget()
        if twidget and ida_kernwin.is_idaview(twidget):
            rt = ida_kernwin.get_view_renderer_type(twidget)
            if rt != ida_kernwin.TCCRT_FLAT:
                return True

        w = target
        while w is not None:
            if w.metaObject().className() == "QOpenGLWidget":
                return True
            p = w.parent()
            if not isinstance(p, QWidget):
                break
            w = p
    except Exception:
        pass
    return False


def _apply_zoom(widget, size):
    """Apply font size via setFont + setStyleSheet + row-height fixups."""
    font = widget.font()
    font.setPointSize(size)
    row_h = QFontMetrics(font).height() + zoom_config.row_extra_height

    widget.setFont(font)
    vp = widget.viewport()
    if vp:
        vp.setFont(font)

    widget.setStyleSheet(
        f"* {{ font-size: {size}pt; }} "
        f"QTreeView::item  {{ height: {row_h}px; }} "
        f"QTableView::item {{ height: {row_h}px; }} "
        f"QListView::item  {{ height: {row_h}px; }}"
    )

    if isinstance(widget, QTableView):
        try:
            vh = widget.verticalHeader()
            if vh:
                vh.setDefaultSectionSize(row_h)
                vh.setMinimumSectionSize(row_h)
        except Exception:
            pass

    if isinstance(widget, QTreeView):
        try:
            widget.setUniformRowHeights(False)
        except Exception:
            pass


def _zoom_percent(size):
    return int(round((size / zoom_config.default_font_size) * 100))

class ZoomBadge(QWidget):
    """Holds badge widget state"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._timestamp = 0.0
        self._zoom_percent = 100
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._start_fade)
        self._timer = QTimer(self)
        self._timer.setInterval(overlay_config.tick_ms)
        self._timer.timeout.connect(self._tick)
        self._bg, self._fg, self._border = self._overlay_colors()
        self._font = QFont()
        self._font.setBold(overlay_config.font_bold)

        self.setAttribute(WA_TRANSPARENT_FOR_MOUSE, True)
        self.setAttribute(WA_TRANSLUCENT_BACKGROUND, True)

        window = parent.window().windowHandle()

        if window is not None and _qt_is_valid(window):
            screen = window.screen()
            window.screenChanged.connect(self._update_metrics)
        else:
            screen = QApplication.primaryScreen()

        self._update_metrics(screen)
        self.hide()

    def _update_metrics(self, screen):
        scale = 1.0
        if screen is not None and _qt_is_valid(screen):
            size = screen.size()
            scale = min(1.0, size.width() / overlay_config.reference_width,
                        size.height() / overlay_config.reference_height)

        self._font.setPointSizeF(overlay_config.font_size * scale)
        self._top_padding = round(overlay_config.top_padding * scale)
        self._right_padding = round(overlay_config.right_padding * scale)
        self._radius = overlay_config.radius * scale
        self.resize(round(overlay_config.width * scale), round(overlay_config.height * scale))
        self._place()
        self.update()

    def show_zoom(self, timestamp: float, zoom_percent: int):
        self._timestamp = timestamp
        self._zoom_percent = zoom_percent
        self._hold_timer.stop()
        self._timer.stop()
        parent = self.parentWidget()
        if parent is None or not _qt_is_valid(parent):
            return

        self._bg, self._fg, self._border = self._overlay_colors()
        self._place()
        self.show()
        self.raise_()
        self.update()
        self._hold_timer.start(int(overlay_config.hold_seconds * 1000))

    def _start_fade(self):
        if not self.isVisible():
            return
        self._timer.start()

    def _tick(self):
        if time.monotonic() - self._timestamp >= self._lifetime():
            self._timer.stop()
            self.hide()
            return

        self._place()
        self.update()

    def paintEvent(self, event):
        age = time.monotonic() - self._timestamp
        if age <= overlay_config.hold_seconds:
            alpha = 1.0
        else:
            alpha = max(0.0, 1.0 - ((age - overlay_config.hold_seconds) / overlay_config.fade_seconds))

        painter = QPainter(self)
        painter.setRenderHint(ANTIALIASING, True)
        painter.setOpacity(alpha)

        painter.setPen(self._border)
        painter.setBrush(self._bg)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1),
                                self._radius, self._radius)

        painter.setFont(self._font)
        painter.setPen(self._fg)
        painter.drawText(QRect(0, 0, self.width(), self.height()), ALIGN_CENTER,
                         f"{self._zoom_percent}%")

    def _place(self):
        parent = self.parentWidget()
        if parent is None or not _qt_is_valid(parent):
            return

        x = max(0, parent.width() - self.width() - self._right_padding)
        y = max(0, min(self._top_padding, parent.height() - self.height()))
        self.move(x, y)


    @staticmethod
    def _overlay_colors():
        tw = ida_kernwin.find_widget("Output window")
        widget = ida_kernwin.PluginForm.TWidgetToPyQtWidget(tw)

        palette = widget.palette()

        bg = QColor(palette.color(OVERLAY_BASE))
        fg = QColor(palette.color(OVERLAY_TEXT))

        bg.setAlpha(overlay_config.background_alpha)
        fg.setAlpha(255)
        border = QColor(fg)
        border.setAlpha(overlay_config.border_alpha)
        return bg, fg, border

    @staticmethod
    def _lifetime() -> float:
        return overlay_config.hold_seconds + overlay_config.fade_seconds


class ZoomOverlay:
    """Stores and manages badge instances"""

    def __init__(self):
        self._badges = {}

    def add(self, widget: QWidget, timestamp: float, font_size: int):
        parent = self._overlay_parent(widget)
        if parent is None or not _qt_is_valid(parent):
            return

        key = self._make_key(parent)
        badge = self._badges.get(key)
        if badge is None or not _qt_is_valid(badge):
            badge = ZoomBadge(parent)
            self._badges[key] = badge
            try:
                parent.destroyed.connect(lambda _=None, k=key: self._badges.pop(k, None))
            except RuntimeError:
                pass

        badge.show_zoom(timestamp, _zoom_percent(font_size))

    def shutdown(self):
        for badge in list(self._badges.values()):
            if _qt_is_valid(badge):
                badge.hide()
                badge.deleteLater()
        self._badges.clear()

    @staticmethod
    def _overlay_parent(widget: QWidget):
        if isinstance(widget, QAbstractScrollArea):
            viewport = widget.viewport()
            if viewport is not None:
                return viewport
        return widget

    @staticmethod
    def _make_key(widget: QWidget) -> int:
        return _qt_pointer(widget)


class WheelZoomFilter(QObject):
    """Application-level event filter — intercepts Ctrl+Wheel globally."""

    def __init__(self):
        super().__init__()
        self._overlay = ZoomOverlay()

    def eventFilter(self, obj, event):
        if event.type() != WHEEL_EVENT:
            return False
        if not (event.modifiers() & CTRL_MODIFIER):
            return False

        delta = event.angleDelta().y()
        if delta == 0:
            return False

        target = QApplication.widgetAt(_global_pos(event))
        if target is None:
            return False

        view = _find_scroll_area(target)
        if not isinstance(view, QAbstractScrollArea):
            return False

        if _is_graph_view(target, view):
            return False

        cur = view.property(SIZE_PROP)
        if not isinstance(cur, int) or cur <= 0:
            ps = view.font().pointSize()
            cur = ps if ps > 0 else zoom_config.default_font_size

        new = max(zoom_config.min_font_size, min(zoom_config.max_font_size,
                  cur + (zoom_config.step if delta > 0 else -zoom_config.step)))
        if new == cur:
            self._overlay.add(view, time.monotonic(), cur)
            return True

        view.setProperty(SIZE_PROP, new)
        _apply_zoom(view, new)
        self._overlay.add(view, time.monotonic(), new)
        return True

    def shutdown(self):
        self._overlay.shutdown()


# -----------------------------------------------------------------------
# Plugin
# -----------------------------------------------------------------------

class ZoomAllViewsPlugin(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_KEEP
    comment = "Ctrl+Scroll font zoom in all views"
    help = "Ctrl+MouseWheel to zoom text in any IDA view"
    wanted_name = PLUGIN_NAME
    wanted_hotkey = PLUGIN_HOTKEY

    def init(self):
        self._filter = None
        self._active = False

        try:
            app = QApplication.instance()
            if app is None:
                return ida_idaapi.PLUGIN_SKIP
        except Exception:
            return ida_idaapi.PLUGIN_SKIP

        self._activate()

        ida_kernwin.msg(
            f"[{PLUGIN_NAME}] v{__version__} loaded  |  "
            f"{PLUGIN_HOTKEY}  |  Edit -> Plugins -> {PLUGIN_NAME}  |  "
            f"Qt: {QT_MODULE}\n"
        )
        return ida_idaapi.PLUGIN_KEEP

    def run(self, arg):
        """Called by IDA when menu item or hotkey is triggered."""
        if self._active:
            self._deactivate()
        else:
            self._activate()

    def term(self):
        self._deactivate()

    def _activate(self):
        if self._active:
            return
        app = QApplication.instance()
        if not app:
            return
        self._filter = WheelZoomFilter()
        app.installEventFilter(self._filter)
        self._active = True
        ida_kernwin.msg(f"[{PLUGIN_NAME}] \u2714 Activated  |  Ctrl+Scroll to zoom\n")

    def _deactivate(self):
        if not self._active:
            return
        try:
            app = QApplication.instance()
            if app and self._filter:
                app.removeEventFilter(self._filter)
                self._filter.shutdown()
        except Exception:
            pass
        self._filter = None
        self._active = False
        ida_kernwin.msg(f"[{PLUGIN_NAME}] \u2718 Deactivated\n")


def PLUGIN_ENTRY():
    return ZoomAllViewsPlugin()
