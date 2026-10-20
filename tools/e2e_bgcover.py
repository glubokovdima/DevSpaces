"""Живой тест «Учитывать окна на фоне»: окно без фокуса на месте панели должно прятать панель.

Мышь не нужна. Тестовое окно ставится на область панели, затем сворачивается; в конце закрывается.
Если на месте панели уже лежит ваше окно, тест сообщает об этом и ничего не проверяет.
"""
import ctypes, os, subprocess, sys, time
from pathlib import Path
u = ctypes.windll.user32
u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import devspaces as ds

W, H = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
panel = (W - 220, H // 4, 210, H // 2)  # правая полоса основного экрана, физические px
me = os.getpid()
if ds.any_window_blocks(panel, me):
    raise SystemExit("на месте панели уже лежит окно — сверните его и запустите снова")

WIN = """
import sys
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import QTimer
a = QApplication(sys.argv); w = QWidget(); w.setWindowTitle("DS BgCover Test"); w.setStyleSheet("background:#36a")
w.show(); QTimer.singleShot(30000, a.quit); a.exec()
"""
p = subprocess.Popen([sys.executable, "-c", WIN])
hw = 0
for _ in range(50):
    hw = u.FindWindowW(None, "DS BgCover Test")
    if hw and u.IsWindowVisible(hw):
        break
    time.sleep(0.1)
u.SetWindowPos(hw, 0, W - 700, H // 5, 650, H // 2, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE
time.sleep(0.6)
ok = True
r1 = ds.any_window_blocks(panel, me)
print("окно на месте панели:", "OK" if r1 else "FAIL"); ok &= r1
u.ShowWindow(hw, 6); time.sleep(0.6)  # SW_MINIMIZE
r2 = not ds.any_window_blocks(panel, me)
print("окно свёрнуто:", "OK" if r2 else "FAIL"); ok &= r2
u.ShowWindow(hw, 9); u.SetWindowPos(hw, 0, 20, 20, 400, 300, 0x0004 | 0x0010); time.sleep(0.6)  # SW_RESTORE, в сторону
r3 = not ds.any_window_blocks(panel, me)
print("окно отодвинуто:", "OK" if r3 else "FAIL"); ok &= r3
p.kill()
print("ИТОГ:", "OK" if ok else "FAIL")
