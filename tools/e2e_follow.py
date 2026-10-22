"""Тест «перенёс окно — перешёл на стол» без мыши: временный стол, тестовое окно, перенос вызовом move_window.

Перед запуском закрыть работающую DevSpaces. Экран дважды переключится на временный стол и обратно.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel
import devspaces as ds
from pyvda import AppView, VirtualDesktop, get_virtual_desktops
app = QApplication(sys.argv); app.setQuitOnLastWindowClosed(False)
d = ds.DevSpaces(app); d.settings["mode"] = "always"
before = [(x.number, x.name) for x in get_virtual_desktops()]
start = VirtualDesktop.current().number
res = []
w = QLabel("DevSpaces: тест переноса окна"); w.resize(420, 160)
seq = []
def later(ms, fn): seq.append((ms, fn))

def setup():
    VirtualDesktop.create(); d.poll_desktops()
    w.show(); w.raise_()
    return d.desks[-1][0]
tmp = []
def move():
    tmp.append(setup())
    d.move_window(int(w.winId()), d.cur_id, tmp[0])
def check_follow():
    h = int(w.winId())
    res.append(("перешли на стол, куда ушло окно", VirtualDesktop.current().number == len(get_virtual_desktops())))
    res.append(("окно на новом столе", AppView(h).desktop.number == len(get_virtual_desktops())))
    res.append(("окно в фокусе", ds.user32.GetForegroundWindow() == h))
def back_and_disable():
    VirtualDesktop(start).go(); d.poll_current()
    AppView(int(w.winId())).move(VirtualDesktop(start))
    d.settings["disabled"].append(tmp[0])  # только в памяти
def move_disabled():
    d.move_window(int(w.winId()), d.cur_id, tmp[0])
def check_disabled():
    res.append(("неактивный стол: окно уехало, а мы остались", VirtualDesktop.current().number == start
                and AppView(int(w.winId())).desktop.number == len(get_virtual_desktops())))
def cleanup():
    d.settings["disabled"].remove(tmp[0])
    w.close()
    n = len(get_virtual_desktops())
    VirtualDesktop(n).remove(fallback=VirtualDesktop(start))
    res.append(("столы пользователя не изменились", [(x.number, x.name) for x in get_virtual_desktops()] == before))
    for nm, ok in res: print("OK  " if ok else "FAIL", nm)
    d.tray.hide(); app.quit()

later(2500, move); later(1500, check_follow); later(300, back_and_disable)
later(1200, move_disabled); later(1200, check_disabled); later(300, cleanup)
def run(i=0):
    if i >= len(seq): return
    ms, fn = seq[i]
    QTimer.singleShot(ms, lambda: (fn(), run(i + 1)))
run()
app.exec()
