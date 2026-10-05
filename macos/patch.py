#!/usr/bin/env python3
"""Reversible Antigravity 2.19.1 macOS UI patch. No network or extra packages."""
import argparse
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import pwd
import re
import stat
import struct
import subprocess
import sys
import tempfile
from datetime import datetime
import uuid

PACKAGE = Path(__file__).resolve().parent
DEFAULT_APP = Path("/Applications/Antigravity.app")
STATE_SCHEMA = 1
VERSION = "2.19.1"
ORIGINAL_ASAR_SHA256 = "341234faf45bd1776fd5418a3c288dedc5487ebfcf153f53d75de17cfe15c1de"
# Exact archive from the earlier local translation, before this installer's
# state/backup format existed. Recognition is read-only; never adopt or replace it.
LEGACY_PATCHED_ASAR_SHA256 = "01ce9917421bb5c01cbcfe44f69c974577dde502b13ab106fd3860a1791b1514"
MARKER = "// Antigravity RU preload v1"
MODIFIED_FILES = (
    "Contents/Resources/app.asar", "Contents/Info.plist",
    "Contents/MacOS/Antigravity", "Contents/_CodeSignature/CodeResources",
)
BACKUP_NAME = re.compile(r"backup-[0-9a-f]{32}\.app\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


def original_user():
    value = os.environ.get("SUDO_UID") if os.geteuid() == 0 else None
    uid = int(value) if value and value.isdecimal() else os.getuid()
    return pwd.getpwuid(uid)


def absolute_path(path, user):
    text = str(path)
    if text == "~" or text.startswith("~/"):
        text = user.pw_dir + text[1:]
    return Path(os.path.abspath(text))


def safe_path(path, must_exist=True):
    """Check every component with lstat; never resolve a user-controlled link."""
    path = Path(path)
    for current in (path, *path.parents):
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if must_exist and current == path:
                raise RuntimeError(f"Не найден путь: {path}")
            continue
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"Символические ссылки не поддерживаются: {current}")
        if current != path and not stat.S_ISDIR(mode):
            raise RuntimeError(f"Родительский путь не является папкой: {current}")
    return path


def regular_file(path):
    safe_path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError(f"Нужен обычный файл: {path}")
    return path


def private_directory(path, user):
    safe_path(path, must_exist=False)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        safe_path(directory.parent)
        directory.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(directory, user.pw_uid, user.pw_gid)
    mode = path.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != user.pw_uid or mode.st_mode & 0o077:
        raise RuntimeError("Каталог состояния должен принадлежать исходному пользователю.")
    os.chmod(path, 0o700)


def ensure_private_state(path, user):
    safe_path(path, must_exist=False)
    if not path.exists():
        return
    regular_file(path)
    metadata = path.lstat()
    if metadata.st_uid != user.pw_uid or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
        raise RuntimeError("Файл состояния должен принадлежать пользователю и иметь права 0600.")


def read_state(state_dir, app, user):
    safe_path(state_dir, must_exist=False)
    if state_dir.exists():
        metadata = state_dir.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != user.pw_uid or metadata.st_mode & 0o077:
            raise RuntimeError("Каталог состояния должен принадлежать пользователю и иметь права 0700.")
    state_path = state_dir / "state.json"
    ensure_private_state(state_path, user)
    if not state_path.exists():
        return None
    if state_path.stat().st_size > 128 * 1024:
        raise RuntimeError("Некорректный размер файла состояния.")
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(saved, dict) or saved.get("schema") != STATE_SCHEMA or saved.get("app") != str(app):
        raise RuntimeError("Запись состояния относится к другому приложению или формату.")
    if saved.get("version") != VERSION or saved.get("status") not in {"prepared", "installed", "rolled-back", "restored"}:
        raise RuntimeError("Некорректная версия или состояние установки.")
    if not isinstance(saved.get("backup"), str) or not BACKUP_NAME.fullmatch(saved["backup"]):
        raise RuntimeError("Резервная копия должна находиться непосредственно в каталоге состояния.")
    for field in ("original_files", "patched_files"):
        files = saved.get(field)
        if not isinstance(files, dict) or set(files) != set(MODIFIED_FILES):
            raise RuntimeError("В записи состояния отсутствуют ожидаемые файлы.")
        for record in files.values():
            if not isinstance(record, dict) or not isinstance(record.get("sha256"), str) or not HASH.fullmatch(record["sha256"]):
                raise RuntimeError("Некорректная контрольная сумма в состоянии.")
            if type(record.get("mode")) is not int or not 0 <= record["mode"] <= 0o777:
                raise RuntimeError("Некорректные права файла в состоянии.")
    if saved["original_files"][MODIFIED_FILES[0]]["sha256"] != ORIGINAL_ASAR_SHA256:
        raise RuntimeError("Состояние не соответствует поддерживаемому исходному архиву.")
    return saved


def save_state(state_dir, state, user):
    private_directory(state_dir, user)
    ensure_private_state(state_dir / "state.json", user)
    atomic_write(state_dir / "state.json", json.dumps(state, ensure_ascii=False, indent=2).encode(), 0o600, user)


@contextmanager
def state_lock(state_dir, user):
    private_directory(state_dir, user)
    lock = state_dir / ".lock"
    ensure_private_state(lock, user)
    # O_NOFOLLOW closes the final-component link race for the lock file.
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
        created = False
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("Некорректный файл блокировки состояния.")
        if not created and metadata.st_uid != user.pw_uid:
            raise RuntimeError("Файл блокировки должен принадлежать пользователю.")
        if os.geteuid() == 0 and created:
            os.fchown(fd, user.pw_uid, user.pw_gid)
        elif os.geteuid() != 0 and metadata.st_uid != user.pw_uid:
            raise RuntimeError("Файл блокировки должен принадлежать пользователю.")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Другая установка или откат уже выполняется.")
        yield
    finally:
        os.close(fd)


def transfer_owner(root, user):
    if os.geteuid() != 0:
        return
    for directory, subdirs, files in os.walk(root, followlinks=False):
        os.chown(directory, user.pw_uid, user.pw_gid, follow_symlinks=False)
        for name in subdirs + files:
            os.chown(Path(directory) / name, user.pw_uid, user.pw_gid, follow_symlinks=False)


def mutation_tools():
    for name in ("codesign", "ditto", "pgrep"):
        path = Path("/usr/bin") / name
        regular_file(path)
        if not os.access(path, os.X_OK):
            raise RuntimeError(f"Недоступен системный инструмент: {name}")


def app_info(app):
    safe_path(app)
    if not app.is_dir():
        raise RuntimeError("Нужна обычная папка Antigravity.app.")
    info = plistlib.loads(regular_file(app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleShortVersionString") != VERSION:
        raise RuntimeError(f"Поддерживается Antigravity {VERSION}; установлена версия {info.get('CFBundleShortVersionString')}.")
    minimum = info.get("LSMinimumSystemVersion")
    if minimum:
        def release(value):
            return tuple(int(x) for x in value.split(".")[:3]) + (0,) * (3 - len(value.split(".")[:3]))
        if release(platform.mac_ver()[0]) < release(minimum):
            raise RuntimeError(f"Приложению требуется macOS {minimum} или новее.")
    return info


def writable_app(app):
    for relative in MODIFIED_FILES:
        path = regular_file(app / relative)
        if not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK):
            raise RuntimeError("Нет прав записи в приложение. Повтори команду с sudo.")
    if not os.access(app, os.W_OK):
        raise RuntimeError("Нет прав записи в приложение. Повтори команду с sudo.")


def records(app):
    return {relative: {"sha256": file_hash(regular_file(app / relative)), "mode": stat.S_IMODE((app / relative).stat().st_mode) & 0o777}
            for relative in MODIFIED_FILES}


def verify_records(root, expected):
    for relative, record in expected.items():
        path = regular_file(root / relative)
        if file_hash(path) != record["sha256"]:
            raise RuntimeError(f"Файлы изменились или повреждены: {path}. Действие отменено.")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    with path.open("rb") as f:
        h = hashlib.sha256()
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
        return h.hexdigest()


def atomic_write(path, data, mode=0o644, owner=None):
    safe_path(path.parent)
    safe_path(path, must_exist=False)
    if path.exists():
        regular_file(path)
    fd, tmp = tempfile.mkstemp(prefix=".antigravity-ru-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if owner is not None and os.geteuid() == 0:
            os.chown(tmp, owner.pw_uid, owner.pw_gid)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Asar:
    def __init__(self, data):
        self.data = data
        _, header_size, _, json_size = struct.unpack("<4I", data[:16])
        self.header = json.loads(data[16:16 + json_size])
        self.base = 8 + header_size

    def entry(self, name):
        item = self.header
        for part in name.split("/"):
            item = item["files"][part]
        return item

    def read(self, name):
        item = self.entry(name)
        start = self.base + int(item["offset"])
        return self.data[start:start + item["size"]]

    def replace(self, updates):
        header = copy.deepcopy(self.header)
        parts = []
        offset = 0

        def walk(directory, prefix=""):
            nonlocal offset
            for name, entry in directory["files"].items():
                full = prefix + name
                if "files" in entry:
                    walk(entry, full + "/")
                elif "offset" in entry and not entry.get("unpacked"):
                    content = updates.get(full, self.read(full))
                    entry["offset"] = str(offset)
                    entry["size"] = len(content)
                    if "integrity" in entry and full in updates:
                        block_size = entry["integrity"].get("blockSize", 4 * 1024 * 1024)
                        entry["integrity"] = {
                            "algorithm": "SHA256", "hash": digest(content),
                            "blockSize": block_size,
                            "blocks": [digest(content[i:i + block_size]) for i in range(0, len(content), block_size)] or [digest(b"")]
                        }
                    parts.append(content)
                    offset += len(content)
        walk(header)
        encoded = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()
        payload = struct.pack("<I", len(encoded)) + encoded
        payload += b"\0" * (-len(payload) % 4)
        header_pickle = struct.pack("<I", len(payload)) + payload
        return struct.pack("<II", 4, len(header_pickle)) + header_pickle + b"".join(parts), digest(encoded)


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
    runtime = regular_file(PACKAGE / "ui-runtime.js").read_text(encoding="utf-8")
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
        "Speech", "Start Speaking", "Stop Speaking"
    ]
    native_dictionary = {key: dictionary[key] for key in labels if key in dictionary}
    addition = "\n    // Antigravity RU native menu; command IDs and handlers stay intact.\n"
    addition += "    const agRuLabels = " + json.dumps(native_dictionary, ensure_ascii=False) + ";\n"
    addition += "    const agRuMenu = (items) => { for (const item of items) { if (Object.hasOwn(agRuLabels, item.label)) item.label = agRuLabels[item.label]; if (item.submenu) agRuMenu(item.submenu.items); } };\n"
    addition += "    agRuMenu(menu.items);\n"
    menu = menu.replace(anchor, addition + anchor, 1)
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


def running(app):
    pattern = "^" + re.escape(str(app / "Contents/MacOS/Antigravity")) + "($| )"
    result = subprocess.run(["/usr/bin/pgrep", "-f", pattern], capture_output=True)
    if result.returncode not in (0, 1):
        raise RuntimeError("Не удалось определить, запущен ли Antigravity.")
    return result.returncode == 0


def require_local_signature(app):
    result = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=2", str(app)], capture_output=True, text=True)
    details = result.stdout + "\n" + result.stderr
    if result.returncode != 0 or not re.search(r"^Signature=adhoc$", details, re.MULTILINE) or not re.search(r"^TeamIdentifier=not set$", details, re.MULTILINE):
        raise RuntimeError("Этот вариант патча поддерживает сборку с локальной ad-hoc подписью. Официально подписанную сборку он не изменяет.")


def install(app, state_dir, user):
    info = app_info(app)
    saved = read_state(state_dir, app, user)
    info_path = app / "Contents/Info.plist"
    asar_path = app / "Contents/Resources/app.asar"
    original = regular_file(asar_path).read_bytes()
    if saved is None and digest(original) == LEGACY_PATCHED_ASAR_SHA256:
        print("[OK] Русский интерфейс уже установлен ранним локальным патчем. Файлы не изменены.")
        print("[i] Повторная установка не нужна. Эта установка не зарегистрирована в публичном установщике.")
        print("[i] Для отката нужна резервная копия от той ранней установки; пункт «Откат» её автоматически не подключает.")
        return
    mutation_tools()
    writable_app(app)
    if saved and saved["status"] == "installed":
        verify_records(app, saved["patched_files"])
        print("[OK] Русский патч уже установлен. Перезапусти Antigravity.")
        return
    if saved and saved["status"] == "prepared":
        raise RuntimeError("Предыдущая установка не завершена. Сохранена резервная копия; повторная установка остановлена.")
    if running(app):
        raise RuntimeError("Полностью закрой Antigravity через Cmd+Q, затем повтори установку.")
    require_local_signature(app)
    if digest(original) != ORIGINAL_ASAR_SHA256:
        raise RuntimeError("Архив не совпадает с поддерживаемой исходной сборкой или известным ранним переводом. Выбери «Статус / совместимость» для подробностей. Файлы не изменены.")
    dictionary = json.loads(regular_file(PACKAGE / "ru.json").read_text(encoding="utf-8"))
    if not isinstance(dictionary, dict) or not dictionary or not all(isinstance(k, str) and isinstance(v, str) for k, v in dictionary.items()):
        raise RuntimeError("Некорректный словарь перевода.")
    patched, header_hash = make_patch(original, dictionary)
    original_files = records(app)
    info["ElectronAsarIntegrity"] = {"Resources/app.asar": {"algorithm": "SHA256", "hash": header_hash}}
    patched_info = plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=False)
    private_directory(state_dir, user)
    if saved:
        snapshot = state_dir / ("record-" + uuid.uuid4().hex + ".json")
        atomic_write(snapshot, json.dumps(saved, ensure_ascii=False, indent=2).encode(), 0o600, user)
    backup = state_dir / ("backup-" + uuid.uuid4().hex + ".app")
    backup.mkdir(mode=0o700)
    print("[i] Сохраняю полную резервную копию приложения…", flush=True)
    subprocess.run(["/usr/bin/ditto", str(app), str(backup)], check=True)
    os.chmod(backup, 0o700)
    transfer_owner(backup, user)
    verify_records(backup, original_files)
    verify_records(app, original_files)
    anticipated_files = copy.deepcopy(original_files)
    anticipated_files[MODIFIED_FILES[0]]["sha256"] = digest(patched)
    anticipated_files[MODIFIED_FILES[1]]["sha256"] = digest(patched_info)
    state = {
        "schema": STATE_SCHEMA, "status": "prepared", "version": VERSION,
        "app": str(app), "backup": backup.name, "created_at": datetime.now().isoformat(timespec="seconds"),
        "original_files": original_files, "patched_files": anticipated_files,
        "dictionary_entries": len(dictionary),
    }
    save_state(state_dir, state, user)
    if running(app):
        state["status"] = "rolled-back"
        save_state(state_dir, state, user)
        raise RuntimeError("Antigravity был запущен во время подготовки. Установка отменена.")
    try:
        atomic_write(asar_path, patched, original_files[MODIFIED_FILES[0]]["mode"])
        atomic_write(info_path, patched_info, original_files[MODIFIED_FILES[1]]["mode"])
        # The original app already uses an ad-hoc signature; preserve its metadata.
        # Nested binaries are unchanged, so only the outer bundle is signed.
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--preserve-metadata=identifier,entitlements,flags,runtime", str(app)], check=True)
        state["status"] = "installed"
        state["patched_files"] = records(app)
        save_state(state_dir, state, user)
    except BaseException:
        print("Установка прервана; восстанавливаю исходные файлы.", file=sys.stderr)
        verify_records(backup, original_files)
        for relative, record in original_files.items():
            source = regular_file(backup / relative)
            atomic_write(app / relative, source.read_bytes(), record["mode"])
        state["status"] = "rolled-back"
        save_state(state_dir, state, user)
        raise
    print(f"[OK] Патч установлен. В словаре {len(dictionary)} записей. Перезапусти Antigravity.")
    print(f"[i] Резервная копия: {backup}")


def restore(app, state_dir, user):
    mutation_tools()
    app_info(app)
    writable_app(app)
    state = read_state(state_dir, app, user)
    if state is None:
        if file_hash(regular_file(app / MODIFIED_FILES[0])) == LEGACY_PATCHED_ASAR_SHA256:
            raise RuntimeError("Найден ранний локальный русский патч. Для его отката нужна резервная копия от ранней установки; публичный установщик не имеет записи о ней. Файлы не изменены.")
        raise RuntimeError("Не найдена запись об установке и резервной копии.")
    if state.get("status") == "restored":
        verify_records(app, state["original_files"])
        print("[OK] Патч уже снят.")
        return
    if state["status"] != "installed":
        raise RuntimeError("Нет подходящей завершённой установки для отката.")
    if running(app):
        raise RuntimeError("Полностью закрой Antigravity через Cmd+Q, затем повтори откат.")
    backup = safe_path(state_dir / state["backup"])
    if not backup.is_dir():
        raise RuntimeError("Резервная копия не является папкой приложения.")
    verify_records(app, state["patched_files"])
    verify_records(backup, state["original_files"])
    before = state_dir / ("before-restore-" + uuid.uuid4().hex)
    private_directory(before, user)
    # Flat private recovery files avoid trusting any directory paths from JSON.
    for index, relative in enumerate(MODIFIED_FILES):
        source = regular_file(app / relative)
        atomic_write(before / str(index), source.read_bytes(), 0o600, user)
    if running(app):
        raise RuntimeError("Antigravity был запущен во время подготовки. Откат отменён.")
    try:
        for relative, record in state["original_files"].items():
            source = regular_file(backup / relative)
            atomic_write(app / relative, source.read_bytes(), record["mode"])
        state["status"] = "restored"
        save_state(state_dir, state, user)
    except BaseException:
        for index, relative in enumerate(MODIFIED_FILES):
            source = regular_file(before / str(index))
            if file_hash(source) != state["patched_files"][relative]["sha256"]:
                raise RuntimeError("Аварийная копия изменена; автоматическое восстановление остановлено.")
            atomic_write(app / relative, source.read_bytes(), state["patched_files"][relative]["mode"])
        raise
    print("[OK] Исходное приложение восстановлено. Резервные копии сохранены.")


def status(app, state_dir, user):
    info = app_info(app)
    current = file_hash(regular_file(app / MODIFIED_FILES[0]))
    state = read_state(state_dir, app, user)
    print(f"Приложение: {app}")
    print(f"Версия: {info.get('CFBundleShortVersionString')}")
    print(f"Каталог состояния: {state_dir}")
    if state is None:
        if current == LEGACY_PATCHED_ASAR_SHA256:
            print("[OK] Русский интерфейс уже установлен ранним локальным патчем.")
            print("Архив совпадает с известной ранней установкой; повторное применение не требуется.")
            print("Записи и резервной копии публичного установщика нет. Автоматический откат этой установки недоступен.")
            return
        print("Патч не зарегистрирован.")
        print("Исходный архив поддерживается." if current == ORIGINAL_ASAR_SHA256 else "Архив отличается от поддерживаемой сборки.")
        if current != ORIGINAL_ASAR_SHA256:
            print(f"SHA-256 архива: {current}")
        try:
            require_local_signature(app)
            print("Локальная подпись поддерживается.")
        except RuntimeError as error:
            print("[!] " + str(error))
        return
    print(f"Состояние: {state['status']}")
    print(f"Резервная копия: {state_dir / state['backup']}")
    expected = state["patched_files"] if state["status"] == "installed" else state["original_files"]
    if state["status"] != "prepared":
        verify_records(app, expected)
        print("Зарегистрированные файлы совпадают с состоянием.")


def main():
    parser = argparse.ArgumentParser(description="Русский интерфейс Antigravity 2.19.1 для macOS")
    parser.add_argument("action", choices=["install", "status", "restore"])
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--state-dir", type=Path, help="Каталог резервных копий и состояния (по умолчанию в ~/Library/Application Support/antigravity-russian/state)")
    args = parser.parse_args()
    try:
        if sys.platform != "darwin":
            raise RuntimeError("Этот патч поддерживает только macOS.")
        if sys.version_info < (3, 9):
            raise RuntimeError("Требуется Python 3.9 или новее.")
        user = original_user()
        app = absolute_path(args.app, user)
        state_dir = absolute_path(args.state_dir or Path(user.pw_dir) / "Library/Application Support/antigravity-russian/state", user)
        if state_dir == app or app in state_dir.parents or state_dir in app.parents:
            raise RuntimeError("Каталог состояния должен находиться отдельно от приложения.")
        action = {"install": install, "status": status, "restore": restore}[args.action]
        if args.action == "status":
            action(app, state_dir, user)
        else:
            with state_lock(state_dir, user):
                action(app, state_dir, user)
    except Exception as error:
        print("Ошибка: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
