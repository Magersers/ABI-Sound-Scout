"""Additional recorded variants for the field merchant's sound groups."""
import json
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from domain import load_catalog


class CatalogVariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'sounds').mkdir()
        for name in ('pickup.ogg', 'drop.ogg', 'variant.flac'):
            (self.base / 'sounds' / name).write_bytes(b'clip-fixture')
        self.catalog = {'schema_version': 1, 'items': [], 'sounds': [
            dict(group_id=1, pickup='sounds/pickup.ogg', drop='sounds/drop.ogg')]}

    def write(self):
        (self.base / 'catalog.json').write_text(json.dumps(self.catalog), encoding='utf-8')

    def test_variants_deduplicate_resolved_paths_within_group_and_kind(self):
        self.catalog['sounds'][0]['pickup_variants'] = [
            'sounds/pickup.ogg', 'sounds/variant.flac', 'sounds/./variant.flac']
        self.write()
        _, references = load_catalog(self.base)
        self.assertEqual(len(references), 3)
        self.assertEqual(len([ref for ref in references if ref['kind'] == 'pickup']), 2)
        self.assertEqual({ref['group_id'] for ref in references}, {1})

    def test_variant_cannot_escape_data_directory(self):
        self.catalog['sounds'][0]['drop_variants'] = ['../outside.ogg']
        self.write()
        with self.assertRaisesRegex(ValueError, 'за пределами'):
            load_catalog(self.base)

    def test_missing_variant_is_reported_before_audio_worker_starts(self):
        self.catalog['sounds'][0]['pickup_variants'] = ['sounds/missing.flac']
        self.write()
        with self.assertRaises(FileNotFoundError):
            load_catalog(self.base)

    def test_variant_field_must_be_a_list(self):
        self.catalog['sounds'][0]['pickup_variants'] = 'sounds/variant.flac'
        self.write()
        with self.assertRaisesRegex(ValueError, 'списком'):
            load_catalog(self.base)

    def test_unrelated_catalog_file_cannot_widen_merchant_catalog(self):
        self.write()
        (self.base / 'catalog_all.json').write_text('{invalid unrelated file', encoding='utf-8')
        catalog, references = load_catalog(self.base)
        self.assertEqual(catalog, self.catalog)
        self.assertEqual(len(references), 2)


class AudioFailureTests(unittest.TestCase):
    def test_capture_or_fatal_error_stops_overlay_and_releases_input(self):
        for error in ('error', 'fatal'):
            with self.subTest(error=error):
                scout = app.Scout.__new__(app.Scout)
                scout.closing, scout.active, scout.analyzing, scout.ready = False, True, True, True
                scout.token = 5
                scout.hotkeys = SimpleNamespace(events=queue.Queue(), active=Mock())
                scout.service = SimpleNamespace(events=queue.Queue(), commands=queue.Queue())
                scout.service.events.put((error, scout.token, 'audio failure'))
                scout.root = SimpleNamespace(after=Mock())
                scout.overlay = SimpleNamespace(withdraw=Mock())
                scout.toggle_btn, scout.device_box, scout.refresh_btn = Mock(), Mock(), Mock()
                scout.level, scout.state_text = Mock(), Mock()
                scout.poll()
                self.assertFalse(scout.active)
                self.assertFalse(scout.analyzing)
                scout.hotkeys.active.assert_called_once_with(False)
                scout.overlay.withdraw.assert_called_once()
                if error == 'fatal':
                    self.assertFalse(scout.ready)


if __name__ == '__main__':
    unittest.main()
