"""Verified official release identities shared by all installers."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent

def load_profiles():
    values = json.loads((ROOT / 'profiles.json').read_text(encoding='utf-8'))
    if not isinstance(values, dict) or not values:
        raise RuntimeError('Каталог совместимости пуст или повреждён.')
    for key, profile in values.items():
        if not isinstance(profile, dict):
            raise RuntimeError('Некорректный профиль: ' + str(key))
        system, arch, version = (profile.get(k) for k in ('platform', 'arch', 'version'))
        if (system not in {'macos', 'windows', 'linux'} or arch not in {'x64', 'arm64'}
                or not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
                or key != system + '-' + arch + '-' + version):
            raise RuntimeError('Некорректная версия или архитектура: ' + str(key))
        fields = ['asar_sha256'] + (['exe_sha256'] if system == 'windows' else [])
        for field in fields:
            if not re.fullmatch(r'[a-f0-9]{64}', str(profile.get(field, ''))):
                raise RuntimeError('Некорректная контрольная сумма: ' + str(key))
    return values

def compatible(system, version, archive_hash):
    return any(p['platform'] == system and p['version'] == version and p['asar_sha256'] == archive_hash
               for p in load_profiles().values())
