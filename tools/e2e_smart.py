"""Живой тест умного режима: тестовое окно развёрнуто → панель скрыта, сдвинуто влево → видна; ручка; клик по карточке."""
import ctypes, sys, time, functools
from ctypes import wintypes
print = functools.partial(print, flush=True)
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import QTimer
from pyvda import VirtualDesktop
u = ctypes.windll.user32
app = QApplication(sys.argv)
w = QWidget(); w.setWindowTitle("DS Test Window"); w.setStyleSheet("background:#335")
P = u.FindWindowW(None, "DevSpaces Panel")
def Hh(): return u.FindWindowW(None, "DevSpaces Handle")
vis = lambda h: bool(u.IsWindowVisible(h))
def rect(h):
    r = wintypes.RECT(); u.GetWindowRect(h, ctypes.byref(r)); return r
def focus():
    w.raise_(); w.activateWindow(); u.SetForegroundWindow(int(w.winId()))
steps = []
def step(fn, delay=1.2): steps.append((fn, delay))
def run(i=0):
    if i >= len(steps): w.close(); app.quit(); return
    fn, d = steps[i]
    try: fn()
    except Exception as e: print("ERR", e)
    QTimer.singleShot(int(d * 1000), lambda: run(i + 1))
step(lambda: (w.showMaximized(), focus()))
step(lambda: print("1) окно развёрнуто, фокус наш:", u.GetForegroundWindow() == int(w.winId()), "→ панель скрыта:", not vis(P), "ручка видна:", vis(Hh())), 0.1)
step(lambda: (w.showNormal(), w.setGeometry(100, 100, 700, 500), focus()))
step(lambda: print("2) окно слева → панель видна:", vis(P), "ручка скрыта:", not vis(Hh())), 0.1)
step(lambda: (w.showMaximized(), focus()))
def hover():
    r = rect(Hh()); u.SetCursorPos(r.left + 4, (r.top + r.bottom) // 2)
step(lambda: print("3) снова развёрнуто → скрыта:", not vis(P)), 0.1)
step(hover, 0.8)
step(lambda: print("4) курсор на ручке → панель видна поверх окна:", vis(P)), 0.1)
def click_card2():
    r = rect(P); hh = r.bottom - r.top
    # карточки идут сверху вниз, 2-я из N — по высоте окна панели
    n = 4; y = r.top + int(hh * 1.5 / n); x = (r.left + r.right) // 2
    u.SetCursorPos(x, y); time.sleep(0.15); u.mouse_event(2, 0, 0, 0, 0); time.sleep(0.05); u.mouse_event(4, 0, 0, 0, 0)
step(click_card2, 1.5)
step(lambda: print("5) клик по 2-й карточке → текущий стол:", VirtualDesktop.current().number), 0.1)
step(lambda: (VirtualDesktop(1).go(), u.SetCursorPos(1000, 700)), 1.5)
step(lambda: (focus(),), 1.0)
step(lambda: print("6) вернулись, курсор ушёл, окно развёрнуто → скрыта:", not vis(P)), 0.1)
QTimer.singleShot(800, run)
app.exec()
