"""Offline regressions for optional local audio during public catalog refresh."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings

SOURCE = Path(__file__).resolve().parents[1] / 'tools' / 'download_catalog.py'
spec = importlib.util.spec_from_file_location('scout_local_audio_updater', SOURCE)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class LocalAudioRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        (self.base / 'sounds_local').mkdir()
        self.path = self.base / 'local_audio_references.json'
        # Header/hash validation belongs to the updater; decoding is checked by
        # the audio engine. No decoder dependency is needed for these fixtures.
        self.payload = b'fLaC-reference-fixture'
        self.file = self.base / 'sounds_local/123.flac'
        self.file.write_bytes(self.payload)
        self.entry = {'path': 'sounds_local/123.flac', 'media_id': 123,
                      'sha256': hashlib.sha256(self.payload).hexdigest()}
        self.manifest = {'schema_version': 1, 'game_data_version': '1.0.0.151.22',
                         'verified_at': '2026-11-11T10:00:00+00:00',
                         'events': {'OilUp': dict(self.entry)}}
        self.catalog = {'data_version': '1.0.0.151.10', 'items': [], 'sounds': [
            {'group_id': 1, 'pickup': 'sounds/1_pickup.ogg', 'drop': 'sounds/1_drop.ogg',
             'pickup_url': 'https://abibuilder.com/audio/merchant/OilUp.ogg',
             'drop_url': 'https://abibuilder.com/audio/merchant/OilDown.ogg',
             'pickup_event': 'OilUp', 'drop_event': 'OilDown'}]}

    def save(self):
        self.path.write_text(json.dumps(self.manifest), encoding='utf-8')

    def apply(self):
        self.save()
        return updater.apply_local_audio_references(self.catalog, self.path, self.base)

    def test_exact_event_adds_variant_once_and_keeps_public_primary_and_version(self):
        original = copy.deepcopy(self.catalog)
        self.manifest['events']['UnusedEvent'] = dict(self.entry)
        self.assertEqual(self.apply(), 1)
        self.assertEqual(self.apply(), 1)
        sound = self.catalog['sounds'][0]
        self.assertEqual(sound.pop('pickup_variants'), ['sounds_local/123.flac'])
        self.assertEqual(sound, original['sounds'][0])
        self.assertEqual(self.catalog['data_version'], original['data_version'])
        self.assertEqual(self.catalog['local_game_version'], '1.0.0.151.22')
        self.assertEqual(self.catalog['verified_at'], self.manifest['verified_at'])
        self.assertEqual(self.catalog['local_audio_references'], {
            'game_data_version': '1.0.0.151.22', 'verified_at': self.manifest['verified_at'],
            'events_applied_count': 1})

    def test_traversal_absolute_and_wrong_media_paths_are_rejected(self):
        original = copy.deepcopy(self.catalog)
        for bad_path in ('../123.flac', str(self.file.resolve()), 'sounds_local/../123.flac',
                         'sounds_local/456.flac', 'sounds_local\\123.flac'):
            with self.subTest(path=bad_path), warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter('always')
                self.manifest['events']['OilUp']['path'] = bad_path
                self.assertEqual(self.apply(), 0)
                self.assertTrue(seen)
                self.assertEqual(self.catalog, original)

    def test_missing_wrong_header_and_checksum_leave_public_fallback_usable(self):
        original = copy.deepcopy(self.catalog)
        for problem in ('missing', 'header', 'checksum'):
            with self.subTest(problem=problem), warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter('always')
                self.manifest['events']['OilUp'] = dict(self.entry)
                if problem == 'missing':
                    self.manifest['events']['OilUp'].update(path='sounds_local/456.flac', media_id=456)
                elif problem == 'header':
                    self.file.write_bytes(b'OggS-wrong-format')
                    self.manifest['events']['OilUp']['sha256'] = hashlib.sha256(self.file.read_bytes()).hexdigest()
                else:
                    self.file.write_bytes(self.payload + b'changed')
                self.assertEqual(self.apply(), 0)
                self.assertTrue(seen)
                self.assertEqual(self.catalog, original)

    def test_future_or_invalid_version_does_not_reuse_stale_local_references(self):
        for version in ('1.0.0.151.23', None, 'unknown'):
            with self.subTest(version=version), warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter('always')
                self.catalog['data_version'] = version
                original = copy.deepcopy(self.catalog)
                self.assertEqual(self.apply(), 0)
                self.assertTrue(seen)
                self.assertEqual(self.catalog, original)

    def test_full_refresh_retains_local_variant_without_network_request_for_it(self):
        self.save()
        html = self.base / 'page.html'
        html.write_text('offline fixture', encoding='utf-8')
        raw = {'meta': {'dataVersion': '1.0.0.151.10'},
               'items': [{'id': 10, 'name': 'Oil', 'soundId': 1, 'size': [1, 2],
                          'rarity': 4, 'contactPrice': 6000}],
               'sounds': [{'id': 1, 'pickup': 'OilUp', 'drop': 'OilDown'}]}
        argv = ['download_catalog.py', '--output', str(self.base), '--html', str(html)]
        with patch.object(sys, 'argv', argv), patch.object(updater, 'extract_catalog', return_value=raw), \
                patch.object(updater, 'download', return_value=b'OggS-public-test-clip') as download, redirect_stdout(io.StringIO()):
            self.assertEqual(updater.main(), 0)
        self.assertEqual(download.call_count, 2)
        self.assertTrue(all(call.args[0].startswith('https://abibuilder.com/audio/merchant/')
                            for call in download.call_args_list))
        catalog = json.loads((self.base / 'catalog.json').read_text(encoding='utf-8'))
        self.assertEqual(catalog['sounds'][0]['pickup_variants'], ['sounds_local/123.flac'])
        self.assertEqual(catalog['sounds'][0]['pickup'], 'sounds/1_pickup.ogg')
        self.assertEqual(self.file.read_bytes(), self.payload)


if __name__ == '__main__':
    unittest.main()
