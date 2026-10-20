"""Живой тест размеров плиток и меню карточки (запускает DevSpaces у себя в процессе).

Перед запуском закрыть работающую DevSpaces. Мышь не трогать ~25 с.
Размеры: xl → xs → исходный, снимки панели в папку из аргумента (по умолчанию текущая).
Меню: создаёт временный стол, двигает его выше/ниже и закрывает — остальные столы не меняются.
Запуск с DEVSPACES_NO_EXCLUDE=1, чтобы панель попадала на снимки.
"""
import ctypes, os, sys, time, functools
from pathlib import Path
print = functools.partial(print, flush=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import devspaces as ds
from pyvda import get_virtual_desktops
import mss, mss.tools

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
u = ctypes.windll.user32
app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
d = ds.DevSpaces(app)
dpr = d.dpr
orig_size, orig_mode = d.settings["size"], d.settings["mode"]
d.settings["mode"] = "always"  # только в памяти: панель должна быть видна, даже если окно её перекрывает
before = [(x.number, x.name) for x in get_virtual_desktops()]
results = []


def phys(pt):
    return round(pt.x() * dpr), round(pt.y() * dpr)


def shot(name):
    r = d.panel.frameGeometry()
    with mss.MSS() as s:
        img = s.grab({"left": round(r.x() * dpr) - 10, "top": round(r.y() * dpr) - 10,
                      "width": round(r.width() * dpr) + 20, "height": round(r.height() * dpr) + 20})
        mss.tools.to_png(img.rgb, img.size, output=str(out / name))


def card(i):
    return d.cards[d.desks[i][0]]


def right_click_and_choose(i, label_start):
    """Правый клик по карточке i, затем левый клик по пункту меню, текст которого начинается с label_start."""
    def choose():
        m = QApplication.activePopupWidget()
        if m is None:
            results.append((f"меню на карточке {i + 1}", False)); return
        a = next((a for a in m.actions() if a.text().startswith(label_start)), None)
        if a is None or not a.isEnabled():
            results.append((f"пункт «{label_start}»", False)); m.close(); return
        x, y = phys(m.mapToGlobal(m.actionGeometry(a).center()))
        u.SetCursorPos(x, y); time.sleep(0.1)
        u.mouse_event(2, 0, 0, 0, 0); u.mouse_event(4, 0, 0, 0, 0)
    c = card(i)
    x, y = phys(c.mapToGlobal(c.rect().center()))
    u.SetCursorPos(x, y); time.sleep(0.1)
    QTimer.singleShot(700, choose)
    u.mouse_event(8, 0, 0, 0, 0); u.mouse_event(16, 0, 0, 0, 0)  # правая кнопка


def drag_card(i, j, n=15):
    """Тянем карточку i на место карточки j настоящей мышью, не блокируя цикл событий Qt."""
    a, b = card(i), card(j)
    x1, y1 = phys(a.mapToGlobal(a.rect().center()))
    x2, y2 = phys(b.mapToGlobal(b.rect().center()))
    u.SetCursorPos(x1, y1)
    u.mouse_event(2, 0, 0, 0, 0)
    for k in range(1, n + 1):
        QTimer.singleShot(40 * k, lambda k=k: u.SetCursorPos(round(x1 + (x2 - x1) * k / n), round(y1 + (y2 - y1) * k / n)))
    QTimer.singleShot(40 * n + 300, lambda: u.mouse_event(4, 0, 0, 0, 0))


steps = []
def step(fn, delay=1.5): steps.append((fn, delay))

def run(i=0):
    if i >= len(steps):
        d.settings["mode"] = orig_mode  # set_size сохраняет настройки целиком
        d.set_size(orig_size)
        after = [(x.number, x.name) for x in get_virtual_desktops()]
        results.append(("столы пользователя не изменились", after == before))
        for name, ok in results:
            print("OK  " if ok else "FAIL", name)
        print("ИТОГ:", "OK" if all(ok for _, ok in results) else "FAIL")
        d.tray.hide(); app.quit(); return
    fn, delay = steps[i]
    try:
        fn()
    except Exception as e:
        results.append((f"шаг {i}: {e}", False))
    QTimer.singleShot(int(delay * 1000), lambda: run(i + 1))

def check(name, cond):
    step(lambda: results.append((name, cond())), 0.1)

# размеры
for key in ("xl", "xs"):
    step(lambda key=key: d.set_size(key), 2.5)
    step(lambda key=key: (shot(f"size_{key}.png"),
                          results.append((f"размер {key}: ширина панели {d.panel.width()} = {ds.PANEL_W}",
                                          d.panel.width() == ds.PANEL_W))), 0.3)
step(lambda: d.set_size(orig_size), 2.0)

# меню карточки на временном столе
n0 = len(before)
step(lambda: right_click_and_choose(n0 - 1, "Добавить"), 2.0)
check("добавить стол: стало на 1 больше", lambda: len(d.desks) == n0 + 1)
step(lambda: right_click_and_choose(n0, "Переместить выше"), 2.0)
check("переместить выше: новый стол стал предпоследним",
      lambda: [x.name for x in get_virtual_desktops()][:n0 - 1] == [nm for _, nm in before][:n0 - 1]
      and get_virtual_desktops()[n0].name == before[-1][1])
step(lambda: right_click_and_choose(n0 - 1, "Переместить ниже"), 2.0)
check("переместить ниже: новый стол снова последний",
      lambda: [x.name for x in get_virtual_desktops()][:n0] == [nm for _, nm in before])
step(lambda: drag_card(n0, n0 - 1), 2.0)
check("перетащить карточку: новый стол встал на место предпоследнего",
      lambda: [x.name for x in get_virtual_desktops()][n0] == before[-1][1])
step(lambda: drag_card(n0 - 1, n0), 2.0)
check("перетащить обратно: новый стол снова последний",
      lambda: [x.name for x in get_virtual_desktops()][:n0] == [nm for _, nm in before])
step(lambda: shot("menu_5desks.png"), 0.3)
step(lambda: right_click_and_choose(n0, "Закрыть стол"), 2.0)
check("закрыть стол: снова столько же, сколько было", lambda: len(get_virtual_desktops()) == n0)

QTimer.singleShot(1500, run)
app.exec()
