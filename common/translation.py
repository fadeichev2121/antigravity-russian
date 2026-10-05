"""Shared UI translation builder for supported Antigravity Desktop releases."""
import json
from pathlib import Path
from asar import Asar

DATA = Path(__file__).resolve().parent.parent / "macos"
MARKER = "// Antigravity RU preload v1"

def translated_literals(source, mapping):
    """Replace a short whitelist of complete native UI string literals."""
    for original, translated in mapping.items():
        for quote in ("'", '"'):
            source = source.replace(quote + original + quote, json.dumps(translated, ensure_ascii=False))
    return source


def make_patch(original, dictionary):
    asar = Asar(original)
    preload = asar.read("dist/preload.js").decode()
    if MARKER in preload:
        raise RuntimeError("Этот архив уже содержит русский патч.")
    # Native popup menus bypass the DOM translator. Translate labels before IPC,
    # retaining the IDs used to dispatch the original actions and all other fields.
    context_labels = [
        "Pin", "Unpin", "Archive", "Unarchive", "Split", "Split Right", "Split Down",
        "Replace With New", "Remove From Split", "Open File", "New Terminal",
        "Rename", "Copy", "Delete", "Terminal", "Conversation Name", "Project Name",
        "Copy Path", "Copy Link", "Open in IDE", "Move to Project",
    ]
    context_dictionary = {key: dictionary[key] for key in context_labels if key in dictionary}
    api_anchor = "const electronNativeAPI = {"
    context_anchor = "showContextMenu: (items) => electron_1.ipcRenderer.invoke('window:show-context-menu', items),"
    if preload.count(api_anchor) != 1:
        raise RuntimeError("Изменилась структура предзагрузки; установка отменена.")
    if preload.count(context_anchor) == 1:
        context_hook = "const agRuContextLabels = " + json.dumps(context_dictionary, ensure_ascii=False) + ";\n"
        context_hook += "const agRuContextItems = (items) => Array.isArray(items) ? items.map(item => { if (!item || typeof item !== 'object') return item; const result = {...item}; if (Object.hasOwn(agRuContextLabels, item.label)) result.label = agRuContextLabels[item.label]; if (Array.isArray(item.submenu)) result.submenu = agRuContextItems(item.submenu); return result; }) : items;\n"
        preload = preload.replace(api_anchor, context_hook + api_anchor, 1)
        preload = preload.replace(context_anchor, context_anchor.replace(
            "'window:show-context-menu', items", "'window:show-context-menu', agRuContextItems(items)"), 1)
    elif 'showContextMenu' in preload or json.loads(asar.read('package.json'))['version'] not in {'2.18.1', '2.17.0', '2.16.0', '2.15.1', '2.15.0'}:
        raise RuntimeError("Изменилась структура контекстного меню; установка отменена.")
    runtime = (DATA / "ui-runtime.js").read_text(encoding="utf-8")
    runtime = runtime.replace("__RU_DICTIONARY__", json.dumps(dictionary, ensure_ascii=False))
    updates = {"dist/preload.js": (preload + "\n" + MARKER + "\n" + runtime + "\n").encode()}

    menu = asar.read("dist/menu.js").decode()
    anchor = "    electron_1.Menu.setApplicationMenu(menu);"
    if anchor not in menu:
        raise RuntimeError("Изменилась структура меню; установка отменена.")
    labels = [
        "File", "Edit", "View", "Window", "Help", "About Antigravity", "Services",
        "Hide Antigravity", "Hide Others", "Show All", "Quit Antigravity", "Quit",
        "New Window", "Docs", "Undo", "Redo", "Cut", "Copy", "Paste",
        "Paste and Match Style", "Delete", "Select All", "Reload", "Force Reload",
        "Actual Size", "Zoom In", "Zoom Out", "Toggle Full Screen", "Minimize",
        "Close", "Close Window", "Zoom", "Bring All to Front", "Front",
        "Speech", "Start Speaking", "Stop Speaking", "Substitutions",
        "Show Substitutions", "Smart Quotes", "Smart Dashes", "Text Replacement",
        "Smart Copy/Paste", "Smart Links", "Spelling and Grammar", "Show Spelling and Grammar",
        "Check Document Now", "Check Spelling While Typing", "Check Grammar With Spelling",
        "Correct Spelling Automatically", "Transformations", "Make Upper Case", "Make Lower Case", "Capitalize",
        "Connect to WSL", "Reopen Locally"
    ]
    native_dictionary = {key: dictionary[key] for key in labels if key in dictionary}
    addition = "// Antigravity RU native menu; command IDs and handlers stay intact.\n"
    addition += "const agRuLabels = " + json.dumps(native_dictionary, ensure_ascii=False) + ";\n"
    addition += "const agRuMenu = (items) => { for (const item of items) { if (Object.hasOwn(agRuLabels, item.label)) item.label = agRuLabels[item.label]; if (item.submenu) agRuMenu(item.submenu.items); } };\n"
    setup_anchor = "function setupApplicationMenu(url) {"
    lookup_anchor = "item.label === submenuLabel"
    if menu.count(setup_anchor) != 1 or menu.count(lookup_anchor) != 1:
        raise RuntimeError("Изменилась структура поиска меню; установка отменена.")
    menu = menu.replace(setup_anchor, addition + setup_anchor, 1)
    # A later WSL refresh still looks up 'File'; accept its translated label too.
    menu = menu.replace(lookup_anchor, "(item.label === submenuLabel || item.label === agRuLabels[submenuLabel])", 1)
    menu = menu.replace(anchor, "    agRuMenu(menu.items);\n" + anchor)
    updates["dist/menu.js"] = menu.encode()
    updater = translated_literals(asar.read("dist/updater.js").decode(), {
        "Check for Updates": "Проверить обновления",
        "Checking for Updates...": "Проверка обновлений…",
        "Downloading Update...": "Загрузка обновления…",
        "Restart to Update": "Перезапустить для обновления",
        "No updates available": "Обновлений нет"
    })
    # Action keys and visible labels use the same enum values.
    updates["dist/updater.js"] = updater.encode()
    main = translated_literals(asar.read("dist/main.js").decode(), {
        "New Window": "Новое окно", "No agents running": "Нет запущенных агентов",
        "Quit": "Выйти", "Cancel": "Отмена", "Confirm Quit": "Подтверждение выхода",
        "Are you sure you want to quit?": "Выйти из Antigravity?",
        "There may be agents or background tasks running.": "Возможно, ещё работают агенты или фоновые задачи."
    })
    tick = chr(96)
    old_open = "label: " + tick + "Open $" + "{electron_1.app.getName()}" + tick
    new_open = "label: " + tick + "Открыть $" + "{electron_1.app.getName()}" + tick
    main = main.replace(old_open, new_open)
    updates["dist/main.js"] = main.encode()
    tray = asar.read("dist/tray.js").decode()
    old_count = "(count > 0 ? " + tick + "$" + "{count}" + tick + " : 'No') +\n                    ' agent' +\n                    (count === 1 ? '' : 's') +\n                    ' running'"
    new_count = "(count > 0 ? " + tick + "Запущено агентов: $" + "{count}" + tick + " : 'Нет запущенных агентов')"
    if old_count not in tray:
        raise RuntimeError("Изменилась структура меню агентов; установка отменена.")
    updates["dist/tray.js"] = tray.replace(old_count, new_count).encode()
    return asar.replace(updates)
