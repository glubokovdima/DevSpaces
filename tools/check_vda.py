import sys, traceback
from pyvda import VirtualDesktop, get_virtual_desktops, AppView
try:
    ds = get_virtual_desktops()
    print("count", len(ds))
    for d in ds:
        try: name = d.name
        except Exception as e: name = f"<name err {e}>"
        print(d.number, repr(name), d.id)
    cur = VirtualDesktop.current()
    print("current", cur.number)
    apps = cur.apps_by_z_order()
    print("apps on current:", len(apps))
    for a in apps[:5]:
        print("  hwnd", a.hwnd)
except Exception:
    traceback.print_exc()
