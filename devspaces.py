"""DevSpaces — боковая панель для переключения виртуальных рабочих столов Windows 11."""
import ctypes
import json
import logging
import os
import queue
import sys
import threading
import time
import winreg
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QLockFile, QObject, QParallelAnimationGroup, QPoint,
                            QPropertyAnimation, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QRegion, QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon,
                           QImage, QPainter, QPainterPath, QPen, QPixmap, QTransform)
from PySide6.QtWidgets import (QApplication, QMenu, QScrollArea, QSystemTrayIcon, QToolTip, QVBoxLayout,
                               QWidget)

import mss
import pyvda.pyvda as pyvda_impl
from pyvda import AppView, VirtualDesktop, get_virtual_desktops

APP_NAME = "DevSpaces"
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "devspaces.log"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

MARGIN = 4          # отступ панели от краёв экрана
PAD = 4             # внутренний отступ панели
CARD_TILT = 22      # поворот карточки, градусы: левый край уходит вглубь
HOVER_SCALE = 1.07
# Размер плитки — ширина миниатюры в логических px; остальное считает apply_size
SIZES = {"xs": ("Очень маленький", 56), "s": ("Маленький", 84), "m": ("Средний", 112),
         "l": ("Большой", 140), "xl": ("Очень большой", 168)}


def apply_size(key):
    """Пересчитать размеры карточки и панели под выбранную ширину миниатюры."""
    global THUMB_W, PANEL_W, FACE_SHADOW, TILT_DIST, R_THUMB, PT_NAME, PT_APPS, H_NAME, H_APPS
    THUMB_W = SIZES.get(key, SIZES["s"])[1]
    PANEL_W = THUMB_W + 40
    FACE_SHADOW = 3 + THUMB_W // 56          # поле под тень вокруг миниатюры
    TILT_DIST = round(THUMB_W * 15 / 7)      # перспектива одинаково заметна на любом размере
    R_THUMB = 3 + THUMB_W / 28               # скругление миниатюры
    PT_NAME = 6.5 + THUMB_W / 56             # 84 px → 8 pt, 168 px → 9,5 pt
    PT_APPS = 5.5 + THUMB_W / 84
    H_NAME = round(PT_NAME * 1.65)
    H_APPS = round(PT_APPS * 1.7)


apply_size("s")
RADIUS = 12
GREEN = QColor("#34C759")
DROP_BLUE = QColor("#4DA3FF")  # карточка, на которую сейчас бросят окно

EDGE_DWELL = 0.2    # сколько держать курсор у края, чтобы не срабатывать при работе со скроллбаром VS Code
LEAVE_DELAY = 0.35
HOT_W = 12          # зона-ручка у правого края основного экрана: ширина и высота
HOT_H = 180
EDGE_CORNER = 60    # углы экрана не триггерят панель: там «закрыть окно» и «свернуть всё»
CAPTURE_EVERY_MS = 1000
LIVE_EVERY = 60     # раз в столько секунд пересобираем миниатюры неактивных столов, пока панель видна
SWITCH_SETTLE = 0.8  # анимация переключения столов попадает в снимок, поэтому ждём

FRIENDLY = {
    "code": "VS Code", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox",
    "explorer": "Проводник", "windowsterminal": "Terminal", "telegram": "Telegram",
    "slack": "Slack", "discord": "Discord", "notepad": "Блокнот", "cursor": "Cursor",
    "devenv": "Visual Studio", "idea64": "IntelliJ", "pycharm64": "PyCharm", "obsidian": "Obsidian",
    "winword": "Word", "excel": "Excel", "outlook": "Outlook", "powershell": "PowerShell",
    "cmd": "cmd", "spotify": "Spotify",
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
WS_EX_TOPMOST = 0x08
WS_EX_APPWINDOW = 0x40000
WS_EX_NOACTIVATE = 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x11
DWMWA_CLOAKED = 14

log = logging.getLogger(APP_NAME)


# ---------- настройки и автозапуск ----------

MODES = {"always": "Всегда показывать", "smart": "Скрывать, если мешает окну", "auto": "Скрывать всегда"}


def load_settings():
    try:
        s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        s = {}
    s.pop("pinned", None)  # старый флаг до появления режимов
    if s.get("mode") not in MODES:
        s["mode"] = "smart"
    if s.get("size") not in SIZES:
        s["size"] = "s"
    return s


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# ---------- Win32 ----------

_proc_cache = {}


def window_app_name(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    pid = pid.value
    if pid not in _proc_cache:
        name = ""
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = Path(buf.value).stem.split(".")[0]  # WhatsApp.Root -> WhatsApp
            kernel32.CloseHandle(h)
        _proc_cache[pid] = name
    name = _proc_cache[pid]
    if name.lower() == "applicationframehost":  # UWP-приложения живут в общем хосте, имя берём из заголовка
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value.split(" - ")[-1] or "App", pid
    return FRIENDLY.get(name.lower(), name[:1].upper() + name[1:]), pid


def is_cloaked(hwnd):
    v = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(v), 4)
    return v.value != 0


def apply_panel_styles(hwnd):
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    ex = (ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST) & ~WS_EX_APPWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020)  # TOPMOST, NOSIZE|NOMOVE|NOACTIVATE|FRAMECHANGED


def at_trigger_edge(pos, g):
    # Только последние 2 px основного экрана: если справа второй монитор, весь он лежит правее края
    return g.right() - 1 <= pos.x() <= g.right() and g.top() + EDGE_CORNER <= pos.y() <= g.bottom() - EDGE_CORNER


def hot_zone(g):
    return QRect(g.right() + 1 - HOT_W, g.center().y() - HOT_H // 2, HOT_W, HOT_H)


def slide_offset():
    # Сдвиг анимации не должен заезжать на соседний монитор справа
    g = QGuiApplication.primaryScreen().geometry()
    # При разном масштабе экранов Qt оставляет между ними разрыв в логических координатах, поэтому без точного сравнения
    neighbour = any(s.geometry().left() > g.right() for s in QGuiApplication.screens())
    return MARGIN if neighbour else 40


DESKTOP_CLASSES = {"Progman", "WorkerW"}
# Временные окна оболочки: пока они в фокусе, решение о скрытии не меняем
SHELL_TRANSIENT = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "XamlExplorerHostIslandWindow", "ForegroundStaging",
                   "MultitaskingViewFrame", "Windows.UI.Core.CoreWindow", "TopLevelWindowForOverflowXamlIsland",
                   "NotifyIconOverflowWindow"}


def foreground_blocks(panel_rect, own_pid):
    """Заходит ли окно с фокусом на область панели (физические px). None — не знаем, оставить как было."""
    h = user32.GetForegroundWindow()
    if not h:
        return False
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(h, buf, 128)
    if buf.value in DESKTOP_CLASSES:
        return False
    if buf.value in SHELL_TRANSIENT:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    if pid.value == own_pid:
        return None
    if user32.IsIconic(h) or not user32.IsWindowVisible(h) or is_cloaked(h):
        return False
    r = wintypes.RECT()
    # Видимые границы без невидимой рамки-тени: иначе окно у края «задевает» панель на 7 px
    if dwmapi.DwmGetWindowAttribute(wintypes.HWND(h), 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(h, ctypes.byref(r))
    x, y, w, hh = panel_rect
    ix = min(r.right, x + w) - max(r.left, x)
    iy = min(r.bottom, y + hh) - max(r.top, y)
    return ix > 16 and iy > 16


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


def moving_window():
    """Окно, которое пользователь сейчас тащит или тянет за край (системный цикл перемещения), иначе None."""
    h = user32.GetForegroundWindow()
    if not h:
        return None
    gti = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(user32.GetWindowThreadProcessId(h, None), ctypes.byref(gti)):
        return None
    if gti.flags & 0x2 and gti.hwndMoveSize:  # GUI_INMOVESIZE
        return user32.GetAncestor(gti.hwndMoveSize, 2)  # GA_ROOT
    return None


def frame_rect(h):
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(wintypes.HWND(h), 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(h, ctypes.byref(r))
    return r


def window_pid(h):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return pid.value


def left_button_down():
    return bool(user32.GetAsyncKeyState(0x01) & 0x8000)


# ---------- снимки экрана ----------

GRABBER = getattr(mss, "MSS", None) or mss.mss
gdi32 = ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("rest", ctypes.c_uint32 * 5)]


def print_window(hwnd, w, h):
    """Окно рисует себя в память. Работает и для окон неактивного стола, которые Windows не выводит на экран."""
    sdc = user32.GetDC(0)
    mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mdc, 2):  # PW_RENDERFULLCONTENT: нужно для окон на DirectX (Chrome, VS Code)
            return None
        bi = BITMAPINFOHEADER(biSize=40, biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return QImage(buf.raw, w, h, w * 4, QImage.Format_RGB32).copy()
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(0, sdc)


def wallpaper_path():
    buf = ctypes.create_unicode_buffer(520)
    user32.SystemParametersInfoW(0x0073, 520, buf, 0)  # SPI_GETDESKWALLPAPER
    return buf.value


class LiveComposer(QObject):
    """Собирает миниатюру неактивного стола: обои + окна этого стола снизу вверх + панель задач с текущего экрана."""
    done = Signal(str, QImage)

    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.busy = False
        self._wall = (None, None)
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, jobs, width_px, avail_phys):
        self.busy = True
        self.q.put((jobs, width_px, avail_phys))

    def _wallpaper(self, size):
        path = wallpaper_path()
        if self._wall[0] != (path, size.width(), size.height()):
            img = QImage(path) if path else QImage()
            if not img.isNull():
                img = img.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                img = img.copy((img.width() - size.width()) // 2, (img.height() - size.height()) // 2,
                               size.width(), size.height())
            self._wall = ((path, size.width(), size.height()), img)
        return self._wall[1]

    def _compose(self, s, hwnds, width_px, avail):
        mon = next((m for m in s.monitors[1:] if m["left"] == 0 and m["top"] == 0), s.monitors[1])
        W, H = mon["width"], mon["height"]
        k = width_px / W
        canvas = QImage(width_px, round(H * k), QImage.Format_RGB32)
        canvas.fill(QColor(20, 20, 26))
        p = QPainter(canvas)
        p.setRenderHints(QPainter.SmoothPixmapTransform | QPainter.Antialiasing)
        wall = self._wallpaper(canvas.size())
        if not wall.isNull():
            p.drawImage(0, 0, wall)
        for hwnd in reversed(hwnds):  # apps_by_z_order отдаёт верхнее окно первым
            if user32.IsIconic(hwnd):
                continue
            wr, fr = wintypes.RECT(), wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(wr))
            if dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(fr), ctypes.sizeof(fr)) != 0:
                fr = wr
            w, h = wr.right - wr.left, wr.bottom - wr.top
            if w <= 0 or h <= 0 or fr.right <= 0 or fr.bottom <= 0 or fr.left >= W or fr.top >= H:
                continue  # окно целиком на другом мониторе
            img = print_window(hwnd, w, h)
            if img is None:
                continue
            # Рисуем только видимую часть: невидимые рамки-тени окна PrintWindow заливает чёрным
            src = QRectF(fr.left - wr.left, fr.top - wr.top, fr.right - fr.left, fr.bottom - fr.top)
            dst = QRectF(fr.left * k, fr.top * k, (fr.right - fr.left) * k, (fr.bottom - fr.top) * k)
            p.drawImage(dst, img, src)
        # Панель задач одинакова на всех столах — берём её с текущего экрана
        ax, ay, aw, ah = avail
        if aw < W or ah < H:
            shot = s.grab(mon)
            screen = QImage(shot.bgra, shot.width, shot.height, shot.width * 4, QImage.Format_RGB32)
            screen = screen.scaled(canvas.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            clip = QRegion(canvas.rect()).subtracted(QRegion(round(ax * k), round(ay * k), round(aw * k), round(ah * k)))
            p.setClipRegion(clip)
            p.drawImage(0, 0, screen)
        p.end()
        return canvas

    def _run(self):
        with GRABBER() as s:
            while True:
                jobs, width_px, avail = self.q.get()
                for desk_id, hwnds in jobs:
                    try:
                        self.done.emit(desk_id, self._compose(s, hwnds, width_px, avail))
                    except Exception as e:
                        log.warning("live thumbnail failed: %s", e)
                self.busy = False


class Capturer(QObject):
    done = Signal(str, float, QImage)

    def __init__(self):
        super().__init__()
        self.q = queue.Queue(maxsize=1)
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, desk_id, width_px):
        try:
            self.q.put_nowait((desk_id, time.monotonic(), width_px))
        except queue.Full:
            pass

    @staticmethod
    def _grab(s, width_px):
        mon = next((m for m in s.monitors[1:] if m["left"] == 0 and m["top"] == 0), s.monitors[1])
        shot = s.grab(mon)
        raw = shot.bgra
        img = QImage(raw, shot.width, shot.height, shot.width * 4, QImage.Format_RGB32)
        return img.scaledToWidth(width_px, Qt.SmoothTransformation)

    def grab_now(self, width_px):
        """Синхронный снимок в главном потоке: нужен до переключения стола, ждать фоновый поток некогда."""
        if not hasattr(self, "_main_mss"):  # у mss свои дескрипторы на каждый поток
            self._main_mss = GRABBER()
        return self._grab(self._main_mss, width_px)

    def _run(self):
        with GRABBER() as s:
            while True:
                desk_id, t, width_px = self.q.get()
                try:
                    self.done.emit(desk_id, t, self._grab(s, width_px))
                except Exception as e:
                    log.warning("capture failed: %s", e)


# ---------- интерфейс ----------

class Card(QWidget):
    clicked = Signal(str)

    def __init__(self, desk_id, thumb_w, thumb_h):
        super().__init__()
        self.desk_id = desk_id
        self.name = ""
        self.apps = []
        self.active = False
        self.image = None
        self.thumb_w = thumb_w
        self.thumb_h = thumb_h
        self.face_w = thumb_w + 2 * FACE_SHADOW
        self.face_h = thumb_h + 4 + H_NAME + H_APPS + 2 * FACE_SHADOW
        self.setFixedSize(PANEL_W - 2 * PAD - 4, round(self.face_h * HOVER_SCALE) + 4)
        self.setCursor(Qt.PointingHandCursor)
        self.t = 0.0  # 0 — покой, 1 — наведение
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(160)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._on_anim)
        self._face = None
        self._face_key = None
        self.ctrl = None           # DevSpaces: ему карточка отдаёт перетаскивание окна
        self.drop_target = False
        self.lifted = False        # эту карточку сейчас переносят на другое место
        self._press = None
        self._dragging = False

    def _on_anim(self, v):
        self.t = v
        self.update()

    def _hover(self, on):
        self.anim.stop()
        self.anim.setStartValue(self.t)
        self.anim.setEndValue(1.0 if on else 0.0)
        self.anim.start()

    def enterEvent(self, e):
        self._hover(True)

    def leaveEvent(self, e):
        self._hover(False)

    def _transform(self):
        tr = QTransform()
        tr.translate(self.width() / 2, self.height() / 2)
        tr.rotate(CARD_TILT * (1 - 0.5 * self.t), Qt.YAxis, TILT_DIST)
        sc = 1 + (HOVER_SCALE - 1) * self.t
        tr.scale(sc, sc)
        tr.translate(-self.face_w / 2, -self.face_h / 2)
        return tr

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.position()
            self._dragging = False

    def mouseMoveEvent(self, e):
        if self._press is None or not (e.buttons() & Qt.LeftButton):
            return
        if not self._dragging and (e.position() - self._press).manhattanLength() > 6:
            self._dragging = bool(self.ctrl and self.ctrl.begin_card_drag(self))
            if not self._dragging:
                self._press = None
        if self._dragging:
            self.ctrl.update_drag()

    def contextMenuEvent(self, e):
        if self.ctrl:
            self.ctrl.card_menu(self, e.globalPos())

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self._dragging:
            self._dragging = False
            self._press = None
            self.ctrl.end_drag()
            return
        self._press = None
        if self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.desk_id)

    def _render_face(self, dpr):
        """Плоская карточка в pixmap; перспектива накладывается при отрисовке."""
        pm = QPixmap(round(self.face_w * dpr), round(self.face_h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        S = FACE_SHADOW
        thumb = QRectF(S, S, self.thumb_w, self.thumb_h)
        p.setPen(Qt.NoPen)
        for i in range(S, 0, -1):
            p.setBrush(QColor(0, 0, 0, 14))
            p.drawRoundedRect(thumb.adjusted(-i, -i + 1, i, i + 1), R_THUMB + i, R_THUMB + i)
        tpath = QPainterPath()
        tpath.addRoundedRect(thumb, R_THUMB, R_THUMB)
        p.save()
        p.setClipPath(tpath)
        if self.image is not None:
            p.drawImage(thumb, self.image)
        else:
            p.fillRect(thumb, QColor(28, 28, 34))
            p.setPen(QColor(150, 150, 160))
            f = QFont(self.font())
            f.setPointSizeF(PT_APPS)
            p.setFont(f)
            p.drawText(thumb, Qt.AlignCenter, "Нет снимка")
        p.restore()
        p.setBrush(Qt.NoBrush)
        if self.drop_target:
            p.setPen(QPen(DROP_BLUE, 2.5))
            p.drawRoundedRect(thumb.adjusted(1, 1, -1, -1), R_THUMB - 1, R_THUMB - 1)
        elif self.active:
            p.setPen(QPen(GREEN, 2))
            p.drawRoundedRect(thumb.adjusted(1, 1, -1, -1), R_THUMB - 1, R_THUMB - 1)
        else:
            p.setPen(QPen(QColor(255, 255, 255, 45), 1))
            p.drawRoundedRect(thumb.adjusted(0.5, 0.5, -0.5, -0.5), R_THUMB, R_THUMB)

        # Общей подложки нет: подписи лежат на обоях и иконках, поэтому под текстом узкая тёмная плашка по его ширине
        tx, tw = S + 4, self.thumb_w - 8
        y = int(thumb.bottom()) + 4
        f = QFont(self.font())
        f.setPointSizeF(PT_NAME)
        f.setWeight(QFont.DemiBold)
        f2 = QFont(self.font())
        f2.setPointSizeF(PT_APPS)
        apps = " · ".join(self.apps) if self.apps else "Нет окон"
        lines = [(f, QFontMetrics(f).elidedText(self.name, Qt.ElideRight, tw), QRect(tx, y, tw, H_NAME), QColor(255, 255, 255)),
                 (f2, QFontMetrics(f2).elidedText(apps, Qt.ElideRight, tw), QRect(tx, y + H_NAME, tw, H_APPS), QColor(215, 215, 222))]
        text_w = max(QFontMetrics(font).horizontalAdvance(text) for font, text, _, _ in lines)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 185))
        p.drawRoundedRect(QRectF(S, y - 1, text_w + 8, H_NAME + H_APPS + 2), R_THUMB - 1, R_THUMB - 1)
        for font, text, rect, color in lines:
            p.setFont(font)
            p.setPen(QColor(0, 0, 0, 150))
            p.drawText(rect.translated(0, 1), Qt.AlignVCenter | Qt.AlignLeft, text)
            p.setPen(color)
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        p.end()
        return pm

    def paintEvent(self, e):
        key = (self.name, tuple(self.apps), id(self.image), self.active, self.drop_target, self.devicePixelRatioF())
        if key != self._face_key:
            self._face = self._render_face(self.devicePixelRatioF())
            self._face_key = key
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        if self.lifted:
            p.setOpacity(0.45)
        p.setTransform(self._transform())
        p.drawPixmap(0, 0, self._face)
        p.end()


class Panel(QWidget):
    def __init__(self):
        # Без Qt.Tool и WindowDoesNotAcceptFocus: иначе у окна нет ApplicationView и pyvda не может его закрепить.
        # Эти свойства включаются через Win32 уже после закрепления.
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("DevSpaces Panel")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(PAD, PAD, PAD, PAD)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        self.scroll.setStyleSheet("""
            QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical { background: transparent; width: 4px; margin: 0; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,60); border-radius: 2px; min-height: 30px; }
            QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { height: 0; background: none; }
        """)
        self.container = QWidget()
        self.list = QVBoxLayout(self.container)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(2)
        self.list.addStretch(1)
        self.scroll.setWidget(self.container)
        lay.addWidget(self.scroll)

        self.anim = QParallelAnimationGroup(self)
        self.a_op = QPropertyAnimation(self, b"windowOpacity")
        self.a_pos = QPropertyAnimation(self, b"pos")
        for a in (self.a_op, self.a_pos):
            a.setDuration(180)
            a.setEasingCurve(QEasingCurve.OutCubic)
            self.anim.addAnimation(a)
        self.anim.finished.connect(self._anim_done)
        self.target_shown = False
        self.content_h = 400

    def home_geometry(self):
        # Окно по высоте карточек и по центру: прозрачная часть окна может ловить клики, поэтому её минимум
        g = QGuiApplication.primaryScreen().availableGeometry()
        h = min(self.content_h, g.height() - 2 * MARGIN)
        return QRect(g.right() + 1 - PANEL_W - MARGIN, g.top() + (g.height() - h) // 2, PANEL_W, h)

    def fit(self, cards):
        self.content_h = sum(c.height() for c in cards) + self.list.spacing() * max(len(cards) - 1, 0) + 2 * PAD
        if self.isVisible() and self.target_shown and self.anim.state() != QPropertyAnimation.Running:
            self.setGeometry(self.home_geometry())

    def slide_in(self):
        if self.target_shown and self.isVisible():
            return
        self.target_shown = True
        home = self.home_geometry()
        self.anim.stop()
        if not self.isVisible():
            self.setGeometry(home.translated(slide_offset(), 0))
            self.setWindowOpacity(0.0)
            self.show()
            apply_panel_styles(int(self.winId()))
        self.a_op.setStartValue(self.windowOpacity())
        self.a_op.setEndValue(1.0)
        self.a_pos.setStartValue(self.pos())
        self.a_pos.setEndValue(home.topLeft())
        self.anim.start()

    def slide_out(self):
        if not self.target_shown:
            return
        self.target_shown = False
        self.anim.stop()
        self.a_op.setStartValue(self.windowOpacity())
        self.a_op.setEndValue(0.0)
        self.a_pos.setStartValue(self.pos())
        self.a_pos.setEndValue(self.home_geometry().topLeft() + QPoint(slide_offset(), 0))
        self.anim.start()

    def _anim_done(self):
        if not self.target_shown:
            self.hide()


class Handle(QWidget):
    """Видимая полоска у края экрана. Прозрачна для мыши: клики уходят в окно под ней (скроллбар VS Code)."""

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setWindowTitle("DevSpaces Handle")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.hot = False

    def set_hot(self, hot):
        if hot != self.hot:
            self.hot = hot
            self.update()

    def place(self):
        self.setGeometry(hot_zone(QGuiApplication.primaryScreen().geometry()))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(GREEN if self.hot else QColor(255, 255, 255, 110))
        w = 5 if self.hot else 4
        p.drawRoundedRect(QRectF(self.width() - w - 2, 0, w, self.height()), w / 2, w / 2)
        p.end()


def make_tray_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(28, 28, 34))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    p.setBrush(QColor(90, 90, 100))
    p.drawRoundedRect(12, 14, 24, 36, 4, 4)
    p.setBrush(GREEN)
    p.drawRoundedRect(40, 14, 12, 10, 3, 3)
    p.setBrush(QColor(140, 140, 150))
    p.drawRoundedRect(40, 27, 12, 10, 3, 3)
    p.drawRoundedRect(40, 40, 12, 10, 3, 3)
    p.end()
    return QIcon(pm)


# ---------- логика ----------

class DevSpaces(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.settings = load_settings()
        apply_size(self.settings["size"])
        self.panel = Panel()
        self.handle = Handle()
        self.cards = {}            # desk_id -> Card
        self.desks = []            # [(id, number, name)]
        self.thumbs = {}           # desk_id -> QImage, только в памяти
        self.cur_id = None
        self.last_switch = 0.0
        self.edge_since = None
        self.outside_since = None
        self.peek_until = 0.0
        self.pin_ok = False
        self.blocked = False       # умный режим: активное окно заходит на панель
        self.blocked_raw = False
        self.blocked_since = 0.0
        self.drag = None           # окно, которое тащат с миниатюры
        self.card_menu_open = False
        self.win_drag = None       # окно, которое тащат за заголовок к карточке
        self.fg_last = None        # (hwnd, rect, развёрнуто) окна с фокусом до начала перетаскивания
        self.own_pid = os.getpid()

        scr = QGuiApplication.primaryScreen()
        self.dpr = scr.devicePixelRatio()
        g = scr.geometry()
        self.thumb_w = THUMB_W
        self.thumb_h = round(self.thumb_w * g.height() / g.width())

        self.capturer = Capturer()
        self.capturer.done.connect(self.on_capture)
        self.live = LiveComposer()
        self.live.done.connect(self.on_live)
        self.last_live = -LIVE_EVERY

        self._build_tray()
        self.poll_desktops()
        self.poll_apps()
        self._init_window()

        self._timer(300, self.poll_current)
        self._timer(1000, self.poll_desktops)
        self._timer(2000, self.poll_apps)
        self._timer(50, self.poll_mouse)
        self._timer(CAPTURE_EVERY_MS, self.capture_now)
        self._timer(5000, self.live_now)
        QTimer.singleShot(500, self.capture_now)

    def _timer(self, ms, fn):
        t = QTimer(self)
        t.timeout.connect(fn)
        t.start(ms)
        return t

    # --- окно панели ---

    def _init_window(self):
        p = self.panel
        p.setGeometry(p.home_geometry())
        p.setWindowOpacity(0.01)
        p.show()
        hwnd = int(p.winId())
        if not os.environ.get("DEVSPACES_NO_EXCLUDE"):
            ok = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            log.info("exclude from capture: %s", bool(ok))
        self._try_pin(0)

    def _try_pin(self, attempt):
        hwnd = int(self.panel.winId())
        try:
            AppView(hwnd).pin()
            self.pin_ok = True
            log.info("panel pinned via pyvda (attempt %d)", attempt)
        except Exception as e:
            if attempt < 15:  # ApplicationView появляется не сразу после show()
                QTimer.singleShot(200, lambda: self._try_pin(attempt + 1))
                return
            log.warning("pyvda pin failed (%s), fallback: tool window + re-show on desktop change", e)
        apply_panel_styles(hwnd)
        if self.settings["mode"] == "always":
            self.panel.target_shown = False
            self.panel.slide_in()
        else:
            self.panel.target_shown = False
            self.panel.hide()
            self.panel.setWindowOpacity(0.0)

    # --- трей ---

    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("DevSpaces")
        m = QMenu()
        group = QActionGroup(m)
        group.setExclusive(True)
        for mode, title in MODES.items():
            a = QAction(title, m, checkable=True, checked=self.settings["mode"] == mode)
            a.triggered.connect(lambda _=False, mode=mode: self.set_mode(mode))
            group.addAction(a)
            m.addAction(a)
        m.addSeparator()
        sm = m.addMenu("Размер плиток")
        sgroup = QActionGroup(sm)
        sgroup.setExclusive(True)
        for key, (title, _) in SIZES.items():
            a = QAction(title, sm, checkable=True, checked=self.settings["size"] == key)
            a.triggered.connect(lambda _=False, key=key: self.set_size(key))
            sgroup.addAction(a)
            sm.addAction(a)
        m.addSeparator()
        self.act_auto = QAction("Автозапуск", m, checkable=True, checked=autostart_enabled())
        self.act_auto.toggled.connect(self.set_autostart)
        act_quit = QAction("Выход", m)
        act_quit.triggered.connect(self.app.quit)
        m.addAction(self.act_auto)
        m.addSeparator()
        m.addAction(act_quit)
        self.menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self.on_tray)
        self.tray.show()

    def on_tray(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.peek_until = time.monotonic() + 2.5
            self.panel.slide_in()

    def set_size(self, key):
        self.settings["size"] = key
        save_settings(self.settings)
        apply_size(key)
        log.info("tile size: %s (%d px)", key, THUMB_W)
        g = QGuiApplication.primaryScreen().geometry()
        self.thumb_w = THUMB_W
        self.thumb_h = round(self.thumb_w * g.height() / g.width())
        # Карточки фиксированного размера проще пересоздать; старые снимки растянутся до новых
        for c in self.cards.values():
            c.deleteLater()
        self.cards.clear()
        self.desks = []
        self.poll_desktops()
        self.poll_apps()
        self.handle.place()
        if self.panel.isVisible():
            self.panel.setGeometry(self.panel.home_geometry())
        self.capture_now()
        self.refresh_live([d for d, _, _ in self.desks])

    # --- управление столами (меню карточки) ---

    def card_menu(self, card, gpos):
        ids = [d for d, _, _ in self.desks]
        if card.desk_id not in ids:
            return
        i = ids.index(card.desk_id)
        m = QMenu()
        a_add = m.addAction("Добавить рабочий стол")
        m.addSeparator()
        a_up = m.addAction("Переместить выше")
        a_up.setEnabled(i > 0)
        a_down = m.addAction("Переместить ниже")
        a_down.setEnabled(i < len(ids) - 1)
        m.addSeparator()
        fb = ids[i - 1] if i > 0 else (ids[1] if len(ids) > 1 else None)
        a_close = m.addAction(f"Закрыть стол (окна → «{self._name(fb)}»)" if fb else "Закрыть стол")
        a_close.setEnabled(fb is not None)
        self.card_menu_open = True
        try:
            act = m.exec(gpos)
        finally:
            self.card_menu_open = False
        try:
            if act is a_add:
                VirtualDesktop.create()
                log.info("desktop created")
            elif act is a_up or act is a_down:
                self.move_desktop(card.desk_id, i - 1 if act is a_up else i + 1)
            elif act is a_close:
                num, fb_num = self._num(card.desk_id), self._num(fb)
                VirtualDesktop(num).remove(fallback=VirtualDesktop(fb_num))
                log.info("desktop %s removed, windows -> %s", num, fb_num)
        except Exception as e:
            log.warning("desktop action failed: %s", e)
        self.poll_desktops()

    def move_desktop(self, did, index):
        # В pyvda нет обёртки для смены порядка; сигнатура MoveDesktop разная в разных сборках Windows
        vd = VirtualDesktop(self._num(did))._virtual_desktop
        mi = pyvda_impl.managers.manager_internal
        try:
            mi.MoveDesktop(vd, index)
        except TypeError:
            mi.MoveDesktop(vd, 0, index)
        log.info("desktop %s moved to position %d", self._num(did), index + 1)

    def _name(self, did):
        return next((nm for d, _, nm in self.desks if d == did), "?")

    def set_mode(self, mode):
        self.settings["mode"] = mode
        save_settings(self.settings)
        log.info("mode: %s", mode)
        self.outside_since = None
        self.blocked_since = 0.0  # пусть умный режим сразу пересчитает перекрытие

    def set_autostart(self, on):
        try:
            set_autostart(on)
            self.settings["autostart"] = on
            save_settings(self.settings)
            log.info("autostart: %s", on)
        except OSError as e:
            log.warning("autostart failed: %s", e)

    # --- рабочие столы ---

    def poll_desktops(self):
        try:
            desks = [(str(d.id), d.number, d.name or f"Рабочий стол {d.number}") for d in get_virtual_desktops()]
        except Exception as e:
            log.warning("get_virtual_desktops failed: %s", e)
            return
        if desks == self.desks:
            return
        self.desks = desks
        ids = [d[0] for d in desks]
        for did in list(self.cards):
            if did not in ids:
                self.cards.pop(did).deleteLater()
                self.thumbs.pop(did, None)
        for i, (did, num, name) in enumerate(desks):
            card = self.cards.get(did)
            if card is None:
                card = Card(did, self.thumb_w, self.thumb_h)
                card.clicked.connect(self.switch_to)
                card.ctrl = self
                card.image = self.thumbs.get(did)
                self.cards[did] = card
            self.panel.list.removeWidget(card)
            self.panel.list.insertWidget(i, card)
            card.name = name
            card.active = did == self.cur_id
            card.update()
        self.panel.fit([self.cards[d[0]] for d in desks])
        log.info("desktops: %s", [(n, nm) for _, n, nm in desks])
        self.poll_current()

    def poll_current(self):
        try:
            cur = str(VirtualDesktop.current().id)
        except Exception:
            return
        if cur != self.cur_id:
            old = self.cur_id
            self.cur_id = cur
            self.last_switch = time.monotonic()
            for did, c in self.cards.items():
                c.active = did == cur
                c.update()
            if old is not None:
                log.info("current desktop -> %s", self._num(cur))
            QTimer.singleShot(int(SWITCH_SETTLE * 1000) + 100, self.capture_now)
        # Fallback, если закрепление не сработало: окно, скрытое Windows на другом столе, показываем заново
        hwnd = int(self.panel.winId())
        if self.panel.isVisible() and is_cloaked(hwnd):
            log.info("panel cloaked on desktop change, re-showing")
            self.panel.hide()
            self.panel.show()
            apply_panel_styles(hwnd)

    def _num(self, did):
        return next((n for d, n, _ in self.desks if d == did), "?")

    def switch_to(self, did):
        num = self._num(did)
        if did == self.cur_id or num == "?":
            return
        if self.cur_id and time.monotonic() - self.last_switch >= SWITCH_SETTLE:
            try:
                self.on_capture(self.cur_id, time.monotonic(), self.capturer.grab_now(round(self.thumb_w * self.dpr)))
                log.info("pre-switch capture of desktop %s", self._num(self.cur_id))
            except Exception as e:
                log.warning("pre-switch capture failed: %s", e)
        try:
            VirtualDesktop(num).go()
            log.info("switch requested -> %s", num)
        except Exception as e:
            log.warning("switch failed: %s", e)
        self.poll_current()

    def poll_apps(self):
        for did, num, _ in self.desks:
            card = self.cards.get(did)
            if card is None:
                continue
            try:
                views = VirtualDesktop(num).apps_by_z_order(include_pinned=False)
            except Exception:
                continue
            names = []
            for v in views:
                try:
                    name, pid = window_app_name(v.hwnd)
                except Exception:
                    continue
                if pid == self.own_pid or not name or name in names:
                    continue
                names.append(name)
            if names != card.apps:
                card.apps = names
                card.update()

    # --- снимки ---

    def capture_now(self):
        if self.cur_id is None or time.monotonic() - self.last_switch < SWITCH_SETTLE:
            return
        self.capturer.request(self.cur_id, round(self.thumb_w * self.dpr))

    def live_now(self):
        p = self.panel
        if not (p.target_shown and p.isVisible()) or self.live.busy or time.monotonic() - self.last_live < LIVE_EVERY:
            return
        if self.refresh_live([d for d, _, _ in self.desks]):
            self.last_live = time.monotonic()

    def refresh_live(self, dids):
        jobs = []
        for did, num, _ in self.desks:
            if did == self.cur_id or did not in dids:
                continue
            try:
                views = VirtualDesktop(num).apps_by_z_order(include_pinned=False)
            except Exception:
                continue
            jobs.append((did, [v.hwnd for v in views]))
        if not jobs:
            return False
        a = QGuiApplication.primaryScreen().availableGeometry()
        k = self.dpr
        self.live.submit(jobs, round(self.thumb_w * self.dpr),
                         (round(a.x() * k), round(a.y() * k), round(a.width() * k), round(a.height() * k)))
        log.info("live thumbnails requested for %d desktops", len(jobs))
        return True

    # --- перенос окон между столами ---

    def card_at(self, gpos):
        if not self.panel.isVisible():
            return None
        return next((c for c in self.cards.values() if c.rect().contains(c.mapFromGlobal(gpos))), None)

    def set_drop_target(self, card):
        for c in self.cards.values():
            if c.drop_target != (c is card):
                c.drop_target = c is card
                c.update()

    def begin_card_drag(self, card):
        if len(self.desks) < 2:
            return False
        self.drag = {"src": card.desk_id}
        card.lifted = True
        card.setCursor(Qt.ClosedHandCursor)
        card.update()
        log.info("card drag: desktop %s", self._num(card.desk_id))
        return True

    def update_drag(self):
        if not self.drag:
            return
        c = self.card_at(QCursor.pos())
        target = c if c and c.desk_id != self.drag["src"] else None
        self.set_drop_target(target)
        if target:
            QToolTip.showText(QCursor.pos() + QPoint(14, 12),
                              f'«{self._name(self.drag["src"])}» → место {self._num(target.desk_id)}')
        else:
            QToolTip.hideText()

    def end_drag(self):
        c = self.card_at(QCursor.pos())
        self.set_drop_target(None)
        QToolTip.hideText()
        d, self.drag = self.drag, None
        for card in self.cards.values():
            card.setCursor(Qt.PointingHandCursor)
            if card.lifted:
                card.lifted = False
                card.update()
        if d and c and c.desk_id != d["src"]:
            try:
                # Стол встаёт на место того, над которым отпустили; остальные сдвигаются
                self.move_desktop(d["src"], self._num(c.desk_id) - 1)
            except Exception as e:
                log.warning("reorder failed: %s", e)
            self.poll_desktops()

    def move_window(self, hwnd, src, dst):
        try:
            AppView(hwnd).move(VirtualDesktop(self._num(dst)))
        except Exception as e:
            log.warning("move window failed: %s", e)
            return
        log.info("moved %s: desktop %s -> %s", window_app_name(hwnd)[0], self._num(src), self._num(dst))
        self.poll_apps()
        # Снимки обоих столов устарели: окно ушло с одного и появилось на другом
        QTimer.singleShot(400, lambda: (self.capture_now(), self.refresh_live([src, dst])))

    def poll_window_drag(self, pos):
        """Окно тащат за заголовок: показываем панель и, если отпустили над чужой карточкой, переносим окно."""
        h = moving_window()
        if h and window_pid(h) == self.own_pid:
            h = None
        if h:
            if self.win_drag is None:
                prev = self.fg_last if self.fg_last and self.fg_last[0] == h else None
                r = wintypes.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                self.win_drag = {"hwnd": h, "rect": prev[1] if prev else (r.left, r.top, r.right, r.bottom),
                                 "zoomed": prev[2] if prev else False}
            c = self.card_at(pos)
            self.set_drop_target(c if c and c.desk_id != self.cur_id else None)
            return
        if self.win_drag:
            d, self.win_drag = self.win_drag, None
            c = self.card_at(pos)
            self.set_drop_target(None)
            if c and c.desk_id != self.cur_id:
                hw = d["hwnd"]
                l, t, r_, b = d["rect"]
                cur = wintypes.RECT()
                user32.GetWindowRect(hw, ctypes.byref(cur))
                # Тянули за край (изменение размера), а не за заголовок — не переносим
                if not d["zoomed"] and (cur.right - cur.left, cur.bottom - cur.top) != (r_ - l, b - t):
                    return
                # Окно бросили у правого края — возвращаем ему прежнее место и размер на новом столе
                user32.SetWindowPos(hw, 0, l, t, r_ - l, b - t, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE
                if d["zoomed"]:
                    user32.ShowWindow(hw, 3)  # SW_MAXIMIZE
                self.move_window(hw, self.cur_id, c.desk_id)
            return
        fg = user32.GetForegroundWindow()
        if fg:
            r = wintypes.RECT()
            user32.GetWindowRect(fg, ctypes.byref(r))
            self.fg_last = (fg, (r.left, r.top, r.right, r.bottom), bool(user32.IsZoomed(fg)))

    def on_live(self, did, img):
        if did == self.cur_id:  # пока собирали, пользователь перешёл на этот стол — настоящий снимок точнее
            return
        img.setDevicePixelRatio(self.dpr)
        self.thumbs[did] = img
        card = self.cards.get(did)
        if card:
            card.image = img
            card.update()

    def on_capture(self, did, t, img):
        # Снимок, начатый до переключения стола, может показывать уже другой стол — выбрасываем
        if did != self.cur_id or self.last_switch > t:
            return
        img.setDevicePixelRatio(self.dpr)
        first = did not in self.thumbs
        self.thumbs[did] = img
        if first:
            log.info("thumbnail stored for desktop %s (%dx%d)", self._num(did), img.width(), img.height())
        card = self.cards.get(did)
        if card:
            card.image = img
            card.update()

    # --- автоскрытие ---

    def panel_rect_phys(self):
        # Основной экран начинается в (0, 0), поэтому логические px переводятся в физические простым умножением
        r = self.panel.home_geometry()
        k = self.dpr
        return round(r.x() * k), round(r.y() * k), round(r.width() * k), round(r.height() * k)

    def poll_mouse(self):
        pos = QCursor.pos()
        now = time.monotonic()
        p = self.panel
        mode = self.settings["mode"]
        if mode == "smart":
            b = foreground_blocks(self.panel_rect_phys(), self.own_pid)
            if b is not None and b != self.blocked_raw:
                self.blocked_raw = b
                self.blocked_since = now
            # Небольшая задержка, чтобы панель не дёргалась, пока окно тащат мимо неё
            if self.blocked != self.blocked_raw and now - self.blocked_since >= 0.2:
                self.blocked = self.blocked_raw
                log.info("foreground window %s panel", "covers" if self.blocked else "clear of")
            keep_shown = not self.blocked
        else:
            keep_shown = mode == "always"
        self.poll_window_drag(pos)
        if self.win_drag or self.drag or self.card_menu_open:
            keep_shown = True  # пока несут окно, карточки должны быть на виду
        show_handle = not keep_shown and not p.target_shown
        if show_handle != self.handle.isVisible():
            if show_handle:
                self.handle.place()
                self.handle.show()
                hh = int(self.handle.winId())
                apply_panel_styles(hh)
                if not os.environ.get("DEVSPACES_NO_EXCLUDE"):
                    user32.SetWindowDisplayAffinity(hh, WDA_EXCLUDEFROMCAPTURE)
            else:
                self.handle.hide()
        if keep_shown:
            if not p.target_shown:
                p.slide_in()
            self.outside_since = None
            return
        g = QGuiApplication.primaryScreen().geometry()
        if not p.target_shown:
            in_hot = hot_zone(g).contains(pos)
            self.handle.set_hot(in_hot)
            at_edge = (in_hot or at_trigger_edge(pos, g)) and not left_button_down()
            if at_edge:
                self.edge_since = self.edge_since or now
                if now - self.edge_since >= EDGE_DWELL:
                    self.edge_since = None
                    self.outside_since = None
                    p.slide_in()
                    log.info("panel shown (edge)")
            else:
                self.edge_since = None
            return
        zone = p.home_geometry().adjusted(-6, -6, MARGIN + 6, 6)
        if zone.contains(pos) or now < self.peek_until or self.menu.isVisible():
            self.outside_since = None
        else:
            self.outside_since = self.outside_since or now
            if now - self.outside_since >= LEAVE_DELAY:
                self.outside_since = None
                p.slide_out()
                log.info("panel hidden (mouse left) pos=%s zone=%s", pos, zone)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    lock = QLockFile(str(DATA_DIR / "devspaces.lock"))
    if not lock.tryLock(100):
        log.info("already running, exit")
        return 0
    log.info("start, windows build %s", sys.getwindowsversion().build)
    ds = DevSpaces(app)
    rc = app.exec()
    ds.tray.hide()
    log.info("exit")
    return rc


if __name__ == "__main__":
    sys.exit(main())
