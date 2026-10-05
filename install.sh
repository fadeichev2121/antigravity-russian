#!/bin/bash
# MIT License. Downloads only this repository's sources; never app binaries.
set -euo pipefail
umask 077

OWNER="fadeichev2121"
REPO="antigravity-russian"
PRODUCT="Antigravity"
DEFAULT_APP="/Applications/Antigravity.app"
SCRIPT_NAME="antigravity_ru.sh"

usage() {
  cat <<EOF
Русский интерфейс $PRODUCT для macOS

Запуск меню:
  bash $SCRIPT_NAME

Действия:
  bash $SCRIPT_NAME install   — установить перевод
  bash $SCRIPT_NAME status    — статус и совместимость
  bash $SCRIPT_NAME restore   — откат и отключение автовосстановления
  bash $SCRIPT_NAME auto-enable  — включить автовосстановление
  bash $SCRIPT_NAME auto-disable — отключить автовосстановление
  bash $SCRIPT_NAME auto-status  — состояние автовосстановления
  bash $SCRIPT_NAME auto-check   — проверить обновление сейчас
  bash $SCRIPT_NAME --help    — эта справка

Сначала полностью закрой приложение через Cmd+Q.
Нужны macOS, Python 3.9+ и штатные инструменты macOS.
EOF
}
if [ "$#" -gt 0 ]; then
  case "$1" in
    --app|--state-dir|--control-dir|--approve-signature) ACTION="menu" ;;
    *) ACTION="$1"; shift ;;
  esac
else
  ACTION="menu"
fi
case "$ACTION" in
  --help|-h) usage; exit 0 ;;
  menu|install|status|restore|auto-enable|auto-disable|auto-status|auto-check) ;;
  *) printf '[Ошибка] Неизвестное действие: %s\n' "$ACTION" >&2; usage; exit 1 ;;
esac

if [ "$(uname -s)" != "Darwin" ]; then
  printf '[Ошибка] Этот установщик предназначен для macOS.\n' >&2
  exit 1
fi

# Download and menu run as the ordinary user. Elevation is requested only
# when the selected application cannot be changed without it.
if [ "$(id -u)" -eq 0 ]; then
  caller_uid="$(printenv SUDO_UID || true)"
  case "$caller_uid" in
    ''|*[!0-9]*|0)
      printf '[Ошибка] Запусти этот файл из обычного аккаунта, без root.\n' >&2
      exit 1 ;;
  esac
  exec /usr/bin/sudo -u "#$caller_uid" /bin/bash "$0" "$ACTION" "$@"
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf '[Ошибка] Python 3 не найден. Установи Python 3.9+ и повтори запуск.\n' >&2
  exit 1
fi
PYTHON_BIN="$(command -v python3)"
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  printf '[Ошибка] Нужен работающий Python 3.9 или новее.\n' >&2
  exit 1
fi
USER_DIR="$("$PYTHON_BIN" -c 'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
SUPPORT="$USER_DIR/Library/Application Support/$REPO"

# A downloaded standalone install.sh fetches the full source package.
# A clone or extracted package works locally without re-downloading it.
if [ ! -f "$ROOT/manifest.json" ] || [ ! -f "$ROOT/macos/patch.py" ] || [ ! -f "$ROOT/profiles.json" ] || [ ! -f "$ROOT/common/translation.py" ]; then
  "$PYTHON_BIN" - "$SUPPORT" <<'PYDIR'
from pathlib import Path
import os, sys
path = Path(sys.argv[1])
for part in [path] + list(path.parents):
    if part.is_symlink():
        raise SystemExit("Ошибка: папка загрузки содержит символическую ссылку.")
path.mkdir(mode=0o700, parents=True, exist_ok=True)
if not path.is_dir() or path.stat().st_uid != os.getuid():
    raise SystemExit("Ошибка: папка загрузки принадлежит другому пользователю.")
path.chmod(0o700)
PYDIR
  TEMP_PACKAGE="$(mktemp -d "$SUPPORT/download.XXXXXXXX")"
  cleanup_download() {
    "$PYTHON_BIN" - "$TEMP_PACKAGE" "$SUPPORT" <<'PYCLEAN'
from pathlib import Path
import shutil, sys
path, parent = map(Path, sys.argv[1:])
if path.parent == parent and path.name.startswith("download.") and path.is_dir() and not path.is_symlink():
    shutil.rmtree(path)
PYCLEAN
  }
  trap cleanup_download EXIT
  printf '[i] Загружаю файлы русификатора %s из GitHub…\n' "$PRODUCT"
  /usr/bin/curl --fail --location --retry 2 --connect-timeout 15 \
    "https://codeload.github.com/$OWNER/$REPO/tar.gz/refs/heads/main" \
    --output "$TEMP_PACKAGE/source.tar.gz"
  "$PYTHON_BIN" - "$TEMP_PACKAGE/source.tar.gz" "$TEMP_PACKAGE/package" "$REPO" <<'PYEXTRACT'
from pathlib import Path, PurePosixPath
import json, os, sys, tarfile
archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
repo = sys.argv[3]
destination.mkdir(mode=0o700)
with tarfile.open(archive, "r:gz") as source:
    members = source.getmembers()
    if len(members) > 2000 or sum(item.size for item in members) > 25 * 1024 * 1024:
        raise SystemExit("Ошибка: неожиданный размер пакета.")
    for item in members:
        parts = PurePosixPath(item.name).parts
        if not parts or parts[0] != repo + "-main" or ".." in parts:
            raise SystemExit("Ошибка: некорректный путь в пакете.")
        if item.isdir():
            continue
        if not item.isfile() or len(parts) < 2:
            raise SystemExit("Ошибка: ссылки или специальные файлы в пакете не поддерживаются.")
        relative = Path(*parts[1:])
        if relative.is_absolute():
            raise SystemExit("Ошибка: абсолютный путь в пакете.")
        target = destination / relative
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with source.extractfile(item) as data, target.open("xb") as output:
            output.write(data.read())
        target.chmod(0o700 if target.name == "install.sh" else 0o600)
manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
if manifest.get("repository") != repo or manifest.get("format") != 1:
    raise SystemExit("Ошибка: загружен пакет другого проекта.")
for name in ("install.sh", "macos/patch.py", "macos/ru.json", "macos/ui-runtime.js", "common/asar.py", "common/translation.py", "common/profiles.py", "profiles.json", "updater/manager.py", "updater/adapter.py", "updater/package.py", "updater/service.py"):
    if not (destination / name).is_file():
        raise SystemExit("Ошибка: в пакете не хватает файлов.")
PYEXTRACT
  ROOT="$TEMP_PACKAGE/package"
  # The complete package is retained in a private, unique directory for
  # subsequent local use. State and backups are stored separately.
  SAVED_ROOT="$("$PYTHON_BIN" - "$ROOT" "$SUPPORT" <<'PYSAVE'
from pathlib import Path
import os, sys, uuid
source, parent = map(Path, sys.argv[1:])
packages = parent / "packages"
if packages.is_symlink():
    raise SystemExit("Ошибка: папка пакетов является символической ссылкой.")
packages.mkdir(mode=0o700, exist_ok=True)
if not packages.is_dir() or packages.stat().st_uid != os.getuid():
    raise SystemExit("Ошибка: небезопасная папка пакетов.")
packages.chmod(0o700)
target = packages / uuid.uuid4().hex
source.rename(target)
print(target)
PYSAVE
)"
  cleanup_download
  trap - EXIT
  exec /bin/bash "$SAVED_ROOT/install.sh" "$ACTION" "$@"
fi

"$PYTHON_BIN" - "$ROOT/manifest.json" "$REPO" <<'PYMANIFEST'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    manifest = json.load(stream)
if manifest.get("repository") != sys.argv[2] or manifest.get("format") != 1:
    raise SystemExit("Ошибка: установщик и пакет относятся к разным проектам.")
PYMANIFEST

PATCH="$ROOT/macos/patch.py"
run_patch() {
  action="$1"
  shift
  local patch_args=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --control-dir) shift 2 ;;
      --approve-signature) shift ;;
      *) patch_args+=("$1"); shift ;;
    esac
  done
  if [ "${#patch_args[@]}" -gt 0 ]; then
    set -- "${patch_args[@]}"
  else
    set --
  fi
  if [ "$action" = "status" ]; then
    "$PYTHON_BIN" "$PATCH" "$action" "$@"
    return $?
  fi
  selected_app="$("$PYTHON_BIN" - "$DEFAULT_APP" "$@" <<'PYAPP'
import argparse, os, sys
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--app", default=sys.argv[1])
args, _ = parser.parse_known_args(sys.argv[2:])
print(os.path.abspath(os.path.expanduser(args.app)))
PYAPP
)"
  selected_parent="$(/usr/bin/dirname "$selected_app")"
  if [ ! -w "$selected_parent" ] || { [ -d "$selected_app" ] && {
      [ ! -w "$selected_app" ] ||
      [ ! -w "$selected_app/Contents/Resources" ] ||
      [ ! -w "$selected_app/Contents/Info.plist" ] ||
      [ ! -w "$selected_app/Contents/MacOS" ] ||
      [ ! -w "$selected_app/Contents/_CodeSignature" ];
    }; }; then
    printf '[i] Для изменения приложения нужен доступ администратора.\n'
    printf '[i] macOS запросит пароль в Терминале; символы при вводе не видны.\n'
    /usr/bin/sudo "$PYTHON_BIN" "$PATCH" "$action" "$@"
  else
    "$PYTHON_BIN" "$PATCH" "$action" "$@"
  fi
}

run_auto() {
  local operation="$1"
  shift
  "$PYTHON_BIN" "$ROOT/updater/manager.py" "$operation" "$@"
}

perform_action() {
  action="$1"
  shift
  case "$action" in
    auto-enable)
      printf '\nАвтовосстановление запускается от твоего аккаунта и проверяет обновление каждые две минуты.\n'
      printf 'Поддерживаемая новая сборка будет переведена после выхода из Antigravity; для каждой сборки сохраняется отдельная копия.\n'
      printf 'На macOS официальная подпись Google будет заменяться локальной; ослабится проверка происхождения библиотек.\n'
      local approved=0 arg
      for arg in "$@"; do
        if [ "$arg" = --approve-signature ]; then approved=1; fi
      done
      if [ "$approved" -eq 1 ]; then run_auto enable "$@"; return $?; fi
      printf 'Согласен включить автовосстановление с локальной переподписью? Введи «да» или «нет»: '
      if ! IFS= read -r consent; then
        printf '\n[Ошибка] Согласие не получено.\n' >&2; return 1
      fi
      case "$consent" in
        да|Да|ДА|yes|YES|y|Y) run_auto enable --approve-signature "$@" ;;
        *) printf '[i] Включение отменено.\n'; return 0 ;;
      esac
      return $? ;;
    auto-disable) run_auto disable "$@"; return $? ;;
    auto-status) run_auto status "$@"; return $? ;;
    auto-check) run_auto check --manual "$@"; return $? ;;
    restore)
      if run_auto restore --manual "$@"; then return 0; else
        local result=$?
        if [ "$result" -ne 3 ]; then return "$result"; fi
      fi ;;
  esac
  if [ "$action" = "install" ] && [ "$REPO" = "antigravity-russian" ]; then
    printf '\nУстановленное приложение Antigravity будет изменено; резервные файлы для отката сохраняются.\n'
    printf 'Официальная подпись Google, если она есть, будет заменена локальной; ослабится проверка происхождения библиотек.\n'
    printf 'Возможны повторный вход и новые системные разрешения.\n'
    printf 'Откат восстановит исходное приложение и подпись. Подробнее — в README репозитория.\n'
    printf 'Согласен применить патч к установленному Antigravity? Введи «да» или «нет»: '
    if ! IFS= read -r consent; then
      printf '\n[Ошибка] Согласие не получено. Установка отменена.\n' >&2
      return 1
    fi
    case "$consent" in
      да|Да|ДА|yes|YES|y|Y)
        run_patch install --approve-local-signature "$@" ;;
      *) printf '[i] Установка отменена.\n'; return 0 ;;
    esac
  else
    run_patch "$action" "$@"
  fi
}

if [ "$ACTION" != "menu" ]; then
  perform_action "$ACTION" "$@"
  exit $?
fi
if [ ! -t 0 ]; then
  printf '[Ошибка] Меню нужно запускать в Терминале: bash %s\n' "$SCRIPT_NAME" >&2
  exit 1
fi
while true; do
  printf '\n==== Русский интерфейс %s для macOS ====\n' "$PRODUCT"
  if [ -d "$DEFAULT_APP" ]; then
    printf 'Найдено: %s\n' "$DEFAULT_APP"
  else
    printf 'Приложение не найдено: %s\n' "$DEFAULT_APP"
  fi
  printf ' 1) Установить русский интерфейс\n'
  printf ' 2) Статус / совместимость\n'
  printf ' 3) Откат\n'
  printf ' 4) Включить автовосстановление после обновлений\n'
  printf ' 5) Отключить автовосстановление\n'
  printf ' 6) Статус автовосстановления\n'
  printf ' 7) Проверить обновление сейчас\n'
  printf ' 0) Выход\n'
  printf 'Выбор: '
  if ! IFS= read -r choice; then printf '\n'; exit 0; fi
  case "$choice" in
    1) operation="install" ;;
    2) operation="status" ;;
    3) operation="restore" ;;
    4) operation="auto-enable" ;;
    5) operation="auto-disable" ;;
    6) operation="auto-status" ;;
    7) operation="auto-check" ;;
    0) exit 0 ;;
    *) printf '[Ошибка] Выбери пункт от 0 до 7.\n' >&2; continue ;;
  esac
  if perform_action "$operation" "$@"; then
    :
  else
    printf '[Ошибка] Действие не завершено. Причина указана выше.\n' >&2
  fi
done
