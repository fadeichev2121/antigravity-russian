# MIT License. PowerShell 5.1+; save this file as UTF-8 with BOM.
[CmdletBinding()]
param(
    [ValidateSet('menu', 'install', 'status', 'restore')]
    [string]$Action = 'menu',
    [Alias('app')]
    [string]$AppPath,
    [Alias('state-dir')]
    [string]$StateDirectory,
    [switch]$Help
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONIOENCODING = 'utf-8'

function Show-Usage {
    Write-Host @'
Русский интерфейс Antigravity для Windows

  .\install.ps1                       — меню
  .\install.ps1 install               — установить перевод
  .\install.ps1 status                — статус / совместимость
  .\install.ps1 restore               — восстановить исходные файлы
  .\install.ps1 status -AppPath ПАПКА  — указать папку приложения
  .\install.ps1 -Help                 — справка

Параметры: -AppPath ПАПКА, -StateDirectory ПАПКА.
Нужны Windows, PowerShell 5.1+ и Python 3.9+.
Поддерживается обычная установка EXE; MSIX / WindowsApps не поддерживаются.
Сначала полностью закрой Antigravity и его обновление.
'@
}

function Assert-NoReparsePath([string]$Path) {
    $cursor = [System.IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Папка загрузки содержит ссылку или точку повторной обработки.'
            }
            if (-not $item.PSIsContainer) {
                throw 'Путь загрузки не является папкой.'
            }
        }
        $parent = [System.IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
}

function Initialize-PrivateDirectory([string]$Path) {
    Assert-NoReparsePath $Path
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $sid = $identity.User
    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner($sid)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $security.AddAccessRule($rule)
    if (Test-Path -LiteralPath $Path) {
        if ($PSVersionTable.PSVersion.Major -ge 6) {
            $existing = [System.IO.FileSystemAclExtensions]::GetAccessControl([System.IO.DirectoryInfo]::new($Path))
        } else {
            $existing = [System.IO.Directory]::GetAccessControl($Path)
        }
        if ($existing.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) {
            throw 'Папка загрузки принадлежит другому пользователю.'
        }
        # Restrict only this user's download directory, never the app's ACL.
        if ($PSVersionTable.PSVersion.Major -ge 6) {
            [System.IO.FileSystemAclExtensions]::SetAccessControl([System.IO.DirectoryInfo]::new($Path), $security)
        } else {
            [System.IO.Directory]::SetAccessControl($Path, $security)
        }
    } else {
        if ($PSVersionTable.PSVersion.Major -ge 6) {
            [void][System.IO.FileSystemAclExtensions]::CreateDirectory($security, $Path)
        } else {
            [void][System.IO.Directory]::CreateDirectory($Path, $security)
        }
    }
}

function Find-Python {
    $probe = 'import sys; sys.exit(1) if sys.version_info < (3,9) else print(sys.executable)'
    foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $command) { continue }
        # Microsoft Store execution aliases can open a store window instead of Python.
        if ($command.Source -match '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]') { continue }
        $arguments = @('-c', $probe)
        if ($name -eq 'py.exe') { $arguments = @('-3', '-c', $probe) }
        try {
            $found = @(& $command.Source @arguments 2>$null)
            if ($LASTEXITCODE -ne 0 -or $found.Count -ne 1) { continue }
            $candidate = [string]$found[0]
            if ($candidate -match '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]') { continue }
            if ([System.IO.File]::Exists($candidate)) { return $candidate }
        } catch {
            continue
        }
    }
    throw 'Python 3.9+ не найден. Установи Python с python.org и повтори запуск; псевдоним Microsoft Store не подходит.'
}

function Get-SourcePackage([string]$Python, [string]$Root) {
    if ((Test-Path -LiteralPath (Join-Path $Root 'manifest.json') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root 'portable\patch.py') -PathType Leaf)) {
        return $Root
    }
    if (-not $env:LOCALAPPDATA) { throw 'Не определена папка LOCALAPPDATA текущего пользователя.' }
    $support = Join-Path $env:LOCALAPPDATA 'antigravity-russian'
    Initialize-PrivateDirectory $support
    Initialize-PrivateDirectory (Join-Path $support 'packages')
    Write-Host '[i] Загружаю исходные файлы русификатора из GitHub…'
    $download = @'
import json, os, shutil, stat, sys, tempfile, urllib.request, zipfile
from pathlib import Path, PurePosixPath

LIMIT = 25 * 1024 * 1024
support = Path(sys.argv[1])
for path in [support, support / 'packages']:
    for part in [path] + list(path.parents):
        if part.exists():
            attributes = getattr(part.lstat(), 'st_file_attributes', 0)
            if part.is_symlink() or attributes & 0x400:
                raise SystemExit('Ошибка: папка загрузки содержит ссылку.')
temporary = Path(tempfile.mkdtemp(prefix='download.', dir=support))
try:
    archive = temporary / 'source.zip'
    request = urllib.request.Request(
        'https://codeload.github.com/fadeichev2121/antigravity-russian/zip/refs/heads/main',
        headers={'User-Agent': 'antigravity-russian-installer'})
    size = 0
    with urllib.request.urlopen(request, timeout=30) as response, archive.open('xb') as output:
        if not response.geturl().startswith('https://codeload.github.com/'):
            raise SystemExit('Ошибка: неожиданный адрес загрузки.')
        while True:
            block = response.read(65536)
            if not block:
                break
            size += len(block)
            if size > LIMIT:
                raise SystemExit('Ошибка: размер загрузки превышает 25 МБ.')
            output.write(block)
    destination = temporary / 'package'
    destination.mkdir()
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > 2000 or sum(item.file_size for item in entries) > LIMIT:
            raise SystemExit('Ошибка: пакет превышает лимит 2000 файлов / 25 МБ.')
        names = set()
        for item in entries:
            parts = PurePosixPath(item.filename).parts
            if (not parts or parts[0] != 'antigravity-russian-main'
                    or '..' in parts or '\\' in item.filename
                    or PurePosixPath(item.filename).is_absolute()
                    or any(':' in part or part.endswith((' ', '.')) for part in parts)):
                raise SystemExit('Ошибка: небезопасный путь в архиве.')
            kind = stat.S_IFMT(item.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                raise SystemExit('Ошибка: ссылки, специальные и зашифрованные файлы запрещены.')
            if any(part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'} for part in parts):
                raise SystemExit('Ошибка: зарезервированное имя Windows в архиве.')
            name = '/'.join(parts).casefold()
            if name in names or (len(parts) == 1 and not item.is_dir()):
                raise SystemExit('Ошибка: повторяющийся или некорректный путь в пакете.')
            names.add(name)
        for item in entries:
            parts = PurePosixPath(item.filename).parts
            if len(parts) == 1:
                continue
            target = destination.joinpath(*parts[1:])
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as incoming, target.open('xb') as output:
                shutil.copyfileobj(incoming, output, length=65536)
    manifest = json.loads((destination / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('repository') != 'antigravity-russian' or manifest.get('format') != 1:
        raise SystemExit('Ошибка: загружен пакет другого проекта или неподдерживаемого формата.')
    for name in ('portable/patch.py', 'profiles.json',
                 'portable/pe_integrity.py',
                 'common/asar.py', 'common/translation.py', 'macos/ru.json',
                 'common/profiles.py', 'macos/ui-runtime.js',
                 'linux/install.sh', 'windows/install.ps1'):
        if not (destination / name).is_file():
            raise SystemExit('Ошибка: в пакете не хватает файлов.')
    retained = support / 'packages' / temporary.name
    destination.rename(retained)
    print(retained)
except Exception as error:
    raise SystemExit('Ошибка загрузки пакета: ' + str(error))
finally:
    shutil.rmtree(temporary)
'@
    $result = @(& $Python -c $download $support)
    if ($LASTEXITCODE -ne 0 -or $result.Count -ne 1) { throw 'Не удалось подготовить пакет русификатора.' }
    return [string]$result[0]
}

function Invoke-PatchAction([string]$Operation) {
    $arguments = @($script:Patch, $Operation)
    if ($script:AppPath) {
        $arguments += @('--app', [System.IO.Path]::GetFullPath($script:AppPath))
    }
    if ($script:StateDirectory) {
        $arguments += @('--state-dir', [System.IO.Path]::GetFullPath($script:StateDirectory))
    }
    if ($Operation -eq 'install') {
        Write-Host ''
        Write-Host 'Полностью закрой Antigravity и дождись завершения его обновления.'
        Write-Host 'Будет изменён app.asar; исходные файлы сохранятся для отката.'
        Write-Host 'Для поддерживаемых сборок EXE и его подпись сохраняются; меняется только app.asar.'
        Write-Host 'Откат восстановит исходные файлы.'
    }
    # Never elevate automatically, change ExecutionPolicy, take ownership,
    # stop processes or weaken permissions on the installation directory.
    & $script:Python @arguments | Out-Host
    $result = $LASTEXITCODE
    if ($result -ne 0) {
        Write-Host '[Ошибка] Действие не завершено. Причина указана выше.'
        Write-Host 'Если причина — отказ в доступе к папке Antigravity, закрой приложение и повтори запуск из терминала администратора.'
        Write-Host 'Установки MSIX / WindowsApps этим способом не поддерживаются.'
    }
    return $result
}

try {
    if ($Help) { Show-Usage; exit 0 }
    if ($env:OS -ne 'Windows_NT') { throw 'Этот установщик предназначен для Windows.' }
    $script:Python = Find-Python
    $candidateRoot = Split-Path -Parent $PSScriptRoot
    $packageRoot = Get-SourcePackage $script:Python $candidateRoot
    $manifestPath = Join-Path $packageRoot 'manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.repository -ne 'antigravity-russian' -or $manifest.format -ne 1) {
        throw 'Установщик и пакет относятся к разным проектам или формат пакета не поддерживается.'
    }
    $script:Patch = Join-Path $packageRoot 'portable\patch.py'
    if ($Action -ne 'menu') {
        $result = Invoke-PatchAction $Action
        exit $result
    }
    if ([Console]::IsInputRedirected) { throw 'Запусти меню в терминале или укажи действие status / restore.' }
    while ($true) {
        Write-Host ''
        Write-Host '==== Русский интерфейс Antigravity для Windows ===='
        if ($AppPath) { Write-Host ('Папка приложения: ' + $AppPath) }
        else { Write-Host 'Папка приложения: автоматический поиск обычной EXE-установки' }
        Write-Host ' 1) Установить русский интерфейс'
        Write-Host ' 2) Статус / совместимость'
        Write-Host ' 3) Откат'
        Write-Host ' 0) Выход'
        $choice = Read-Host 'Выбор'
        switch ($choice) {
            '1' { $result = Invoke-PatchAction 'install' }
            '2' { $result = Invoke-PatchAction 'status' }
            '3' { $result = Invoke-PatchAction 'restore' }
            '0' { exit 0 }
            default { Write-Host '[Ошибка] Выбери 1, 2, 3 или 0.' }
        }
    }
} catch {
    Write-Host ('[Ошибка] ' + $_.Exception.Message)
    exit 1
}
