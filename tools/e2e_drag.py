"""Живой тест переноса окон между столами через карточки.

1) Тащим окно с миниатюры активного стола на карточку другого стола.
2) Тащим окно за заголовок и бросаем на карточку другого стола — окно должно вернуться на прежнее место.
Запускать при работающей DevSpaces; мышь не трогать ~20 с. Тестовое окно в конце закрывается.
"""
import ctypes, subprocess, sys, time, functools
from ctypes import wintypes
from pathlib import Path
print = functools.partial(print, flush=True)
u = ctypes.windll.user32
u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import QPointF
from PySide6.QtGui import QGuiApplication
import devspaces as ds
ds.apply_size(ds.load_settings()["size"])  # раскладка карточек зависит от выбранного размера
from pyvda import AppView, VirtualDesktop, get_virtual_desktops

app = QGuiApplication([])
dpr = app.primaryScreen().devicePixelRatio()
g = app.primaryScreen().geometry()
W, H = round(g.width() * dpr), round(g.height() * dpr)

WIN = """
import sys
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import QTimer
a = QApplication(sys.argv); w = QWidget(); w.setWindowTitle("DS Drag Test"); w.setStyleSheet("background:#a33")
w.show(); w.windowHandle().setGeometry(%d, %d, %d, %d); QTimer.singleShot(40000, a.quit); a.exec()
"""


def spawn(x, y, w, h):
    # Qt принимает логические px
    p = subprocess.Popen([sys.executable, "-c", WIN % (round(x / dpr), round(y / dpr), round(w / dpr), round(h / dpr))])
    for _ in range(50):
        hw = u.FindWindowW(None, "DS Drag Test")
        if hw and u.IsWindowVisible(hw):
            time.sleep(0.5)
            return p, hw
        time.sleep(0.1)
    raise SystemExit("тестовое окно не появилось")


def rect(h):
    r = wintypes.RECT(); u.GetWindowRect(h, ctypes.byref(r)); return r.left, r.top, r.right, r.bottom


def drag(x1, y1, x2, y2, steps=25):
    u.SetCursorPos(x1, y1); time.sleep(0.15)
    u.mouse_event(2, 0, 0, 0, 0); time.sleep(0.15)
    for i in range(1, steps + 1):
        u.SetCursorPos(round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps)); time.sleep(0.03)
    time.sleep(0.4)
    u.mouse_event(4, 0, 0, 0, 0); time.sleep(1.2)


def card_geom(i):
    """Карточка i (0..): физический прямоугольник виджета карточки на экране."""
    P = u.FindWindowW(None, "DevSpaces Panel")
    l, t, r, b = rect(P)
    thumb_h = round(ds.THUMB_W * g.height() / g.width())
    face_h = thumb_h + 4 + 13 + 11 + 2 * ds.FACE_SHADOW
    ch = round(face_h * ds.HOVER_SCALE) + 4
    cw = ds.PANEL_W - 2 * ds.PAD - 4
    top = ds.PAD + i * (ch + 2)
    return l + ds.PAD * dpr, t + top * dpr, cw, ch, thumb_h, face_h


def card_center(i):
    x, y, cw, ch, _, _ = card_geom(i)
    return round(x + cw * dpr / 2), round(y + ch * dpr / 2)


def thumb_to_screen(i, fx, fy):
    """Точка миниатюры (доли 0..1) → экран, тем же преобразованием, что рисует карточка в покое."""
    x, y, cw, ch, thumb_h, face_h = card_geom(i)
    from PySide6.QtGui import QTransform
    from PySide6.QtCore import Qt
    face_w = ds.THUMB_W + 2 * ds.FACE_SHADOW
    tr = QTransform(); tr.translate(cw / 2, ch / 2); tr.rotate(ds.CARD_TILT, Qt.YAxis, ds.TILT_DIST); tr.translate(-face_w / 2, -face_h / 2)
    pt = tr.map(QPointF(ds.FACE_SHADOW + fx * ds.THUMB_W, ds.FACE_SHADOW + fy * thumb_h))
    return round(x + pt.x() * dpr), round(y + pt.y() * dpr)


cur = VirtualDesktop.current().number
others = [d.number for d in get_virtual_desktops() if d.number != cur]
a, b = others[0], others[1] if len(others) > 1 else others[0]
ok = True

# 1) с миниатюры: окно в левой части экрана, хватаем его на миниатюре активного стола
p, hw = spawn(round(W * 0.05), round(H * 0.15), round(W * 0.45), round(H * 0.6))
u.SetForegroundWindow(hw); time.sleep(1.5)  # дать DevSpaces снять стол с этим окном поверх
sx, sy = thumb_to_screen(cur - 1, 0.25, 0.45)
tx, ty = card_center(a - 1)
drag(sx, sy, tx, ty)
res = AppView(hw).desktop.number
print(f"1) с миниатюры стола {cur} на карточку {a}: окно теперь на столе {res} → {'OK' if res == a else 'FAIL'}")
ok &= res == a
AppView(hw).move(VirtualDesktop(cur)); time.sleep(0.5)

# 2) за заголовок: окно тащим к карточке, отпускаем — оно должно уехать и вернуться на место
u.SetForegroundWindow(hw); time.sleep(0.6)
before = rect(hw)
fr = ds.frame_rect(hw)
tx, ty = card_center(b - 1)
drag((fr.left + fr.right) // 2, fr.top + 12, tx, ty, steps=40)
res = AppView(hw).desktop.number
after = rect(hw)
print(f"2) за заголовок на карточку {b}: окно на столе {res} → {'OK' if res == b else 'FAIL'}; "
      f"место восстановлено: {after == before} ({before} → {after})")
ok &= res == b and after == before

u.SetCursorPos(W // 2, H // 2)
p.kill()
print("ИТОГ:", "OK" if ok else "FAIL")
