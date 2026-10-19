"""Какие окна неактивных столов отдают картинку через PrintWindow."""
import ctypes, sys
from ctypes import wintypes
u = ctypes.windll.user32; g = ctypes.windll.gdi32
u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
from pyvda import VirtualDesktop, get_virtual_desktops
sys.path.insert(0, r"D:\soft\DevSpaces")
class BMI(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32), ("rest", ctypes.c_uint32 * 5)]
def shot(h):
    r = wintypes.RECT(); u.GetWindowRect(h, ctypes.byref(r)); w, hh = r.right - r.left, r.bottom - r.top
    if w <= 0 or hh <= 0: return None, (w, hh)
    sdc = u.GetDC(0); mdc = g.CreateCompatibleDC(sdc); bmp = g.CreateCompatibleBitmap(sdc, w, hh); old = g.SelectObject(mdc, bmp)
    ok = u.PrintWindow(h, mdc, 2)
    bi = BMI(); bi.biSize = 40; bi.biWidth = w; bi.biHeight = -hh; bi.biPlanes = 1; bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * hh * 4); g.GetDIBits(mdc, bmp, 0, hh, buf, ctypes.byref(bi), 0)
    g.SelectObject(mdc, old); g.DeleteObject(bmp); g.DeleteDC(mdc); u.ReleaseDC(0, sdc)
    data = buf.raw
    step = max(1, len(data) // 4 // 4000) * 4
    nonblack = sum(1 for i in range(0, len(data), step) if data[i] > 8 or data[i+1] > 8 or data[i+2] > 8)
    return ok, round(100 * nonblack / (len(data) // step), 1)
import time
cur = VirtualDesktop.current().number
for d in get_virtual_desktops():
    for v in d.apps_by_z_order(include_pinned=False):
        t = ctypes.create_unicode_buffer(80); u.GetWindowTextW(v.hwnd, t, 80)
        import devspaces
        name = devspaces.window_app_name(v.hwnd)[0]
        t0 = time.perf_counter(); ok, pct = shot(v.hwnd); ms = (time.perf_counter() - t0) * 1000
        print(f"стол {d.number}{' (активный)' if d.number == cur else ''}: {name:<14} iconic={u.IsIconic(v.hwnd)} ok={ok} не-чёрного={pct}% {ms:.0f}мс")
