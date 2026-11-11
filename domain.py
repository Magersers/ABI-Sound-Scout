"""Catalog and presentation logic, independent of audio and Windows."""
from pathlib import Path
import json

SHAPES = [(1, 1), (1, 2), (1, 3), (1, 4), (2, 2), (2, 3), (2, 4), (3, 3), (3, 4)]


def load_catalog(base):
    base = Path(base)
    catalog = json.loads((base / 'catalog.json').read_text(encoding='utf-8'))
    if catalog.get('schema_version') != 1:
        raise ValueError('Неподдерживаемая версия каталога')
    refs = []
    groups = set()
    for sound in catalog['sounds']:
        group_id = int(sound['group_id'])
        if group_id in groups:
            raise ValueError('Повторяющаяся звуковая группа в каталоге')
        groups.add(group_id)
        for kind in ('pickup', 'drop'):
            variants = sound.get(kind + '_variants', [])
            if not isinstance(variants, list):
                raise ValueError('Варианты звука должны быть списком путей')
            seen = set()
            for relative in [sound[kind], *variants]:
                path = (base / relative).resolve()
                if not path.is_relative_to(base.resolve()):
                    raise ValueError('Путь звука за пределами папки data')
                if not path.is_file():
                    raise FileNotFoundError(f'Не найден эталон: {path.name}')
                if path not in seen:
                    refs.append({'group_id': group_id, 'kind': kind, 'path': str(path)})
                    seen.add(path)
    if not isinstance(catalog.get('items'), list):
        raise ValueError('В каталоге отсутствует список предметов')
    identifiers = set()
    for item in catalog['items']:
        if not isinstance(item, dict) or not item.get('name') or 'id' not in item:
            raise ValueError('Некорректный предмет в каталоге')
        if item['id'] in identifiers:
            raise ValueError('Повторяющийся предмет в каталоге')
        identifiers.add(item['id'])
        if item.get('sound_group') is not None and item['sound_group'] not in groups:
            raise ValueError('Предмет ссылается на неизвестную звуковую группу')
    return catalog, refs


def candidates(catalog, results, shape=None):
    scores = {}
    for result in results:
        gid = int(result['group_id'])
        scores[gid] = max(scores.get(gid, 0), result['score'])
    rows = []
    for item in catalog['items']:
        if item['sound_group'] not in scores:
            continue
        if shape and sorted((item['width'], item['height'])) != sorted(shape):
            continue
        rows.append(dict(item, score=scores[item['sound_group']]))
    return sorted(rows, key=lambda x: (-x['score'], -(x.get('contact_price') or 0), x['name']))


def money(value):
    return f'{int(value):,}'.replace(',', ' ') if value is not None else '—'
