import json
from pathlib import Path
import sys
import tempfile
import unittest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
from domain import candidates, load_catalog, money


class CatalogTests(unittest.TestCase):
    def test_bundled_catalog_has_complete_unique_sound_and_item_links(self):
        catalog, references = load_catalog(APP_DIR / 'data')
        self.assertEqual(len(references), 156)
        self.assertEqual(len({(r['group_id'], r['kind']) for r in references}), 78)
        groups = {r['group_id'] for r in references}
        self.assertEqual(groups, set(range(1, 40)))
        self.assertEqual(len(catalog['items']), 189)
        self.assertEqual(len({i['id'] for i in catalog['items']}), len(catalog['items']))
        self.assertTrue(all(i['sound_group'] in groups for i in catalog['items']))

    def test_catalog_cannot_reference_file_outside_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            outside = root / 'outside.ogg'
            outside.write_bytes(b'not relevant')
            for injected in ('../outside.ogg', str(outside.resolve())):
                with self.subTest(path=injected):
                    (data / 'catalog.json').write_text(json.dumps({
                        'schema_version': 1,
                        'sounds': [{'group_id': 1, 'pickup': injected, 'drop': injected}],
                        'items': [],
                    }), encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'за пределами'):
                        load_catalog(data)

    def test_missing_audio_is_reported_before_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / 'catalog.json').write_text(json.dumps({
                'schema_version': 1,
                'sounds': [{'group_id': 1, 'pickup': 'absent.ogg', 'drop': 'absent.ogg'}],
                'items': [],
            }), encoding='utf-8')
            with self.assertRaises(FileNotFoundError):
                load_catalog(data)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {'items': [
            {'id': 1, 'name': 'Wide', 'sound_group': 7, 'width': 3, 'height': 2, 'contact_price': 200},
            {'id': 2, 'name': 'Tall', 'sound_group': 7, 'width': 2, 'height': 3, 'contact_price': 100},
            {'id': 3, 'name': 'Square', 'sound_group': 7, 'width': 2, 'height': 2, 'contact_price': None},
            {'id': 4, 'name': 'Other sound', 'sound_group': 8, 'width': 2, 'height': 3, 'contact_price': 900},
        ]}

    def test_rotation_preserves_both_orientations_without_widening_shape(self):
        results = [{'group_id': 7, 'score': .9}]
        expected = [1, 2]
        self.assertEqual([r['id'] for r in candidates(self.catalog, results, (2, 3))], expected)
        self.assertEqual([r['id'] for r in candidates(self.catalog, results, (3, 2))], expected)
        self.assertEqual(candidates(self.catalog, results, (1, 6)), [])

    def test_pickup_and_drop_do_not_duplicate_candidates(self):
        rows = candidates(self.catalog, [
            {'group_id': 7, 'score': .75}, {'group_id': 7, 'score': .91},
            {'group_id': 8, 'score': .8},
        ])
        self.assertEqual([r['id'] for r in rows], [1, 2, 3, 4])
        self.assertTrue(all(r['score'] == .91 for r in rows[:3]))
        self.assertNotIn('score', self.catalog['items'][0])

    def test_unknown_audio_never_falls_back_to_all_items(self):
        self.assertEqual(candidates(self.catalog, []), [])
        self.assertEqual(candidates(self.catalog, [{'group_id': 999, 'score': 1}]), [])

    def test_zero_price_is_distinct_from_missing_price(self):
        self.assertEqual(money(0), '0')
        self.assertEqual(money(None), '—')
        self.assertEqual(money(1234567), '1 234 567')


if __name__ == '__main__':
    unittest.main()
