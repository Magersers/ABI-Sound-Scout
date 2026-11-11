"""Validated opening-key preference, independent of Tk and the input listener."""
import json
import logging
import os
from pathlib import Path
import tempfile


DEFAULT_TOGGLE_KEY = 'F5'
TOGGLE_KEYS = (tuple(f'F{number}' for number in range(1, 13) if number not in (6, 7))
               + tuple(chr(code) for code in range(ord('A'), ord('Z') + 1))
               + ('Insert', 'Home', 'End', 'PageUp', 'PageDown'))
_CANONICAL = {name.casefold(): name for name in TOGGLE_KEYS}
_NAVIGATION_VKS = {'Insert': 0x2D, 'Home': 0x24, 'End': 0x23, 'PageUp': 0x21, 'PageDown': 0x22}


def normalize_toggle_key(name):
    if not isinstance(name, str) or name.strip().casefold() not in _CANONICAL:
        raise ValueError('Выберите F1–F12, кроме F6/F7, латинскую букву A–Z или клавишу навигации.')
    return _CANONICAL[name.strip().casefold()]


def toggle_key_vk(name):
    key = normalize_toggle_key(name)
    if key in _NAVIGATION_VKS:
        return _NAVIGATION_VKS[key]
    if len(key) == 1:
        return ord(key)
    return 0x70 + int(key[1:]) - 1


def settings_path():
    local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    return local / 'ABI Sound Scout' / 'settings.json'


def load_toggle_key(path=None):
    target = Path(path) if path is not None else settings_path()
    try:
        data = json.loads(target.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError('Настройки должны быть JSON-объектом')
        return normalize_toggle_key(data.get('toggle_key', DEFAULT_TOGGLE_KEY))
    except FileNotFoundError:
        return DEFAULT_TOGGLE_KEY
    except (OSError, UnicodeError, ValueError) as exc:
        logging.warning('Opening key preference unavailable; using F5: %s', exc)
        return DEFAULT_TOGGLE_KEY


def save_toggle_key(name, path=None):
    key = normalize_toggle_key(name)
    target = Path(path) if path is not None else settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.settings-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'toggle_key': key}, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
