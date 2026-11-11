"""Offline regressions for preserving localization during source refreshes."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
import warnings


SOURCE = Path(__file__).resolve().parents[1] / 'tools' / 'download_catalog.py'
spec = importlib.util.spec_from_file_location('scout_catalog_updater', SOURCE)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class TranslationRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'item_names_ru.json'
        self.items = [dict(id=10, name='Oil', width=1, height=2, sound_group=1,
                           contact_price=6000, reference_price=15000),
                      dict(id=20, name='Gold Pen', width=1, height=2, sound_group=22,
                           contact_price=400000, reference_price=700000)]

    def write_dictionary(self, entries):
        self.path.write_text(json.dumps(dict(schema_version=1, language='ru', items=entries),
                                        ensure_ascii=False), encoding='utf-8')

    def test_dictionary_wins_and_only_changes_display_names(self):
        before = copy.deepcopy(self.items)
        self.write_dictionary({'10': dict(name_en='Oil', name_ru='Масло')})
        previous = {'items': [dict(id=10, name='Oil', name_ru='Старый перевод'),
                              dict(id=20, name='Gold Pen', name_ru='Золотая ручка')]}
        count = updater.preserve_russian_names(self.items, self.path, previous)
        self.assertEqual(count, 2)
        self.assertEqual(self.items[0]['name_ru'], 'Масло')
        self.assertEqual(self.items[1]['name_ru'], 'Золотая ручка')
        stripped = [{k: v for k, v in item.items() if k != 'name_ru'} for item in self.items]
        self.assertEqual(stripped, before)

    def test_refresh_with_missing_dictionary_retains_same_id_and_name(self):
        previous = {'items': [dict(id=10, name='Oil', name_ru='Масло')]}
        self.assertEqual(updater.preserve_russian_names(self.items, self.path, previous), 1)
        self.assertEqual(self.items[0]['name_ru'], 'Масло')
        self.assertNotIn('name_ru', self.items[1])

    def test_reused_id_or_renamed_english_item_cannot_inherit_translation(self):
        self.write_dictionary({'10': dict(name_en='Old Item', name_ru='Старый предмет'),
                               '20': dict(name_en='Gold Pen', name_ru='Золотая ручка')})
        previous = {'items': [dict(id=10, name='Old Item', name_ru='Старый предмет'),
                              dict(id=999, name='Oil', name_ru='Не тот идентификатор')]}
        self.assertEqual(updater.preserve_russian_names(self.items, self.path, previous), 1)
        self.assertNotIn('name_ru', self.items[0])
        self.assertEqual(self.items[1]['name_ru'], 'Золотая ручка')

    def test_malformed_dictionary_falls_back_to_previous_catalog(self):
        previous = {'items': [dict(id=10, name='Oil', name_ru='Масло')]}
        for text in ('{broken', '[]', '{"schema_version":2,"language":"ru","items":{}}'):
            with self.subTest(text=text):
                self.path.write_text(text, encoding='utf-8')
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    self.assertEqual(updater.preserve_russian_names(self.items, self.path, previous), 1)
                self.assertTrue(caught)
                self.assertEqual(self.items[0]['name_ru'], 'Масло')

    def test_malformed_entries_do_not_discard_other_valid_translations(self):
        self.write_dictionary({'10': dict(name_en='Oil', name_ru=[]),
                               '20': dict(name_en='Gold Pen', name_ru='Золотая ручка')})
        previous = {'items': [dict(id=10, name='Oil', name_ru='Масло')]}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.assertEqual(updater.preserve_russian_names(self.items, self.path, previous), 2)
        self.assertTrue(caught)
        self.assertEqual(self.items[0]['name_ru'], 'Масло')
        self.assertEqual(self.items[1]['name_ru'], 'Золотая ручка')

    def test_complete_offline_refresh_keeps_translation_and_accepts_new_prices(self):
        output = self.path.parent
        previous = {'items': [dict(id=10, name='Oil', name_ru='Масло', contact_price=6000)],
                    'sounds': []}
        (output / 'catalog.json').write_text(json.dumps(previous, ensure_ascii=False), encoding='utf-8')
        html_path = output / 'public_page.html'
        html_path.write_text('saved public page fixture', encoding='utf-8')
        raw = {'items': [dict(id=10, name='Oil', soundId=1, size=[1, 2], rarity=4,
                              contactPrice=6200, referencePrice=15000)],
               'sounds': [dict(id=1, pickup='OilPickup', drop='OilDrop')]}
        argv = ['download_catalog.py', '--output', str(output), '--html', str(html_path)]
        with patch.object(sys, 'argv', argv), patch.object(updater, 'extract_catalog', return_value=raw), \
                patch.object(updater, 'download', return_value=b'OggS-local-test-fixture'), redirect_stdout(io.StringIO()):
            self.assertEqual(updater.main(), 0)
        refreshed = json.loads((output / 'catalog.json').read_text(encoding='utf-8'))
        self.assertEqual(refreshed['items'][0]['name_ru'], 'Масло')
        self.assertEqual(refreshed['items'][0]['contact_price'], 6200)
        self.assertEqual(refreshed['items'][0]['sound_group'], 1)


if __name__ == '__main__':
    unittest.main()
