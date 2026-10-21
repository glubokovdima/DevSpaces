"""Тест переименования стола без мыши: создаёт временный стол, переименовывает через диалог, удаляет.

Перед запуском закрыть работающую DevSpaces. Окно ввода на секунду забирает фокус.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import devspaces as ds
from pyvda import VirtualDesktop, get_virtual_desktops
app = QApplication(sys.argv); app.setQuitOnLastWindowClosed(False)
d = ds.DevSpaces(app); d.settings["mode"] = "always"
before = [(x.number, x.name) for x in get_virtual_desktops()]
res = []
def go():
    VirtualDesktop.create(); d.poll_desktops()
    card = d.cards[d.desks[-1][0]]
    def answer():
        dlg = next((w for w in QApplication.topLevelWidgets() if type(w).__name__ == "QInputDialog" and w.isVisible()), None)
        res.append(("диалог открыт, в поле текущее имя", dlg is not None and dlg.textValue() == card.name))
        res.append(("диалог поверх и активен", dlg is not None and dlg.isActiveWindow() and ds.user32.GetForegroundWindow() == int(dlg.winId())))
        if dlg:
            dlg.setTextValue("  DS тест  "); dlg.accept()
    QTimer.singleShot(800, answer)
    d.rename_desktop(card)
    d.poll_desktops()
    res.append(("имя стола поменялось (пробелы обрезаны)", get_virtual_desktops()[-1].name == "DS тест"))
    res.append(("карточка показывает новое имя", card.name == "DS тест"))
    # отмена ничего не меняет
    QTimer.singleShot(600, lambda: [w.reject() for w in QApplication.topLevelWidgets() if type(w).__name__ == "QInputDialog" and w.isVisible()])
    d.rename_desktop(card)
    res.append(("отмена не меняет имя", get_virtual_desktops()[-1].name == "DS тест"))
    n = len(get_virtual_desktops())
    VirtualDesktop(n).remove(fallback=VirtualDesktop(n - 1))
    res.append(("столы пользователя не изменились", [(x.number, x.name) for x in get_virtual_desktops()] == before))
    for nm, ok in res: print("OK  " if ok else "FAIL", nm)
    d.tray.hide(); app.quit()
QTimer.singleShot(2500, go)
app.exec()
