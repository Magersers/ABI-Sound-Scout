"""Offline packaged-runtime check; never opens a window or an audio device."""
import json
from pathlib import Path
import sys


def run(result_path):
    result_path = Path(result_path)
    report = {'ok': False}
    try:
        import tkinter
        import pyaudiowpatch
        import soundfile as sf
        from domain import load_catalog
        from engine import Matcher
        from hotkey_settings import TOGGLE_KEYS, toggle_key_vk
        from winhotkeys import _KeyState, RI_KEY_BREAK

        base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        catalog, refs = load_catalog(base / 'data')
        matcher = Matcher(refs)
        if matcher.errors or len(matcher.references) != len(refs):
            raise RuntimeError('Failed to decode one or more reference sounds')
        checked = set()
        for ref in refs:
            suffix = Path(ref['path']).suffix
            if suffix in checked:
                continue
            audio, rate = sf.read(ref['path'], dtype='float32')
            matches = matcher.match(audio, rate)
            if not any(r['group_id'] == ref['group_id'] and r['score'] >= .70 for r in matches):
                raise RuntimeError('Reference sound did not match its group')
            checked.add(suffix)
        for name in TOGGLE_KEYS:
            state = _KeyState(name)
            vk = toggle_key_vk(name)
            if state.feed(1, vk, 0, 0) != 'toggle':
                raise RuntimeError('Configured toggle key did not activate')
            if state.feed(1, vk, 0, 0) is not None:
                raise RuntimeError('Held toggle key repeated')
            state.feed(1, vk, 0, RI_KEY_BREAK)
        tcl = tkinter.Tcl()
        report.update(ok=True, items=len(catalog['items']), groups=len(catalog['sounds']),
                      references=len(refs), audio_formats=sorted(checked),
                      toggle_keys=len(TOGGLE_KEYS), tcl_version=tcl.eval('info patchlevel'),
                      frozen=bool(getattr(sys, 'frozen', False)))
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['ok'] else 1
