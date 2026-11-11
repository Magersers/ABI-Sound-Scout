"""Offline regression coverage for versioned, verified local sound corrections."""
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


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "download_catalog.py"
spec = importlib.util.spec_from_file_location("scout_sound_updater", SOURCE)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
AUDIO = b"OggS-verified-local-audio-fixture"
HASH = hashlib.sha256(AUDIO).hexdigest()


def sound(group_id, stem):
    entry = {"group_id": group_id}
    for action, ending in (("pickup", "Up"), ("drop", "Down")):
        entry[action] = f"sounds/{group_id}_{action}.ogg"
        entry[action + "_event"] = f"Item_{stem}_{ending}"
        entry[action + "_url"] = f"https://abibuilder.com/audio/merchant/Item_{stem}_{ending}.ogg"
        entry[action + "_sha256"] = HASH
    return entry


class SoundOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        (self.output / "sounds").mkdir()
        self.path = self.output / "local_catalog_overrides.json"
        self.catalog = {
            "data_version": "1.0.0.151.17",
            "items": [{"id": 408080029, "name": "Gem Necklace", "name_ru": "Ожерелье",
                       "sound_group": 33, "width": 1, "height": 1, "contact_price": 4000},
                      {"id": 498080019, "name": "Star of Kamona Pendant", "sound_group": 33}],
            "sounds": [sound(33, "GoldChain")],
        }
        self.overrides = {"schema_version": 1, "game_data_version": "1.0.0.151.22",
                          "items": {}, "sounds": [sound(39, "Necklace")]}
        for item in self.catalog["items"]:
            self.overrides["items"][str(item["id"])] = {
                "name_en": item["name"],
                "expected_pickup_event": "Item_GoldChain_Up",
                "expected_drop_event": "Item_GoldChain_Down",
                "pickup_event": "Item_Necklace_Up", "drop_event": "Item_Necklace_Down",
            }

    def write_overrides(self):
        self.path.write_text(json.dumps(self.overrides), encoding="utf-8")

    def test_exact_matches_change_only_bindings_and_add_one_verified_pair(self):
        before = copy.deepcopy(self.catalog)
        self.overrides["sounds"].append(sound(40, "Unused"))
        self.write_overrides()
        self.assertEqual(updater.apply_local_sound_overrides(self.catalog, self.path), 2)
        self.assertEqual([item["sound_group"] for item in self.catalog["items"]], [39, 39])
        for original, updated in zip(before["items"], self.catalog["items"]):
            self.assertEqual({**updated, "sound_group": 33}, original)
        self.assertEqual([s["group_id"] for s in self.catalog["sounds"]], [33, 39])
        self.assertEqual(self.catalog["data_version"], before["data_version"])
        self.assertEqual(self.catalog["local_sound_overrides"]["applied_item_ids"], ["408080029", "498080019"])
        after = copy.deepcopy(self.catalog)
        self.assertEqual(updater.apply_local_sound_overrides(self.catalog, self.path), 0)
        self.assertEqual(self.catalog, after)

    def test_reused_ids_names_and_changed_upstream_pairs_are_left_alone(self):
        self.write_overrides()
        for field, value in (("id", 123), ("name", "Renamed Necklace"), ("sound_group", 50)):
            with self.subTest(field=field):
                catalog = copy.deepcopy(self.catalog)
                catalog["items"] = [catalog["items"][0]]
                catalog["items"][0][field] = value
                catalog["sounds"].append(sound(50, "NewUpstreamMapping"))
                before = copy.deepcopy(catalog)
                self.assertEqual(updater.apply_local_sound_overrides(catalog, self.path), 0)
                self.assertEqual(catalog, before)

    def test_newer_or_unknown_upstream_version_disables_local_override(self):
        self.write_overrides()
        for version in ("1.0.0.151.23", "1.0.0.152.1", None, "unknown"):
            with self.subTest(version=version):
                catalog = copy.deepcopy(self.catalog)
                catalog["data_version"] = version
                before = copy.deepcopy(catalog)
                with self.assertWarns(RuntimeWarning):
                    self.assertEqual(updater.apply_local_sound_overrides(catalog, self.path), 0)
                self.assertEqual(catalog, before)
        self.catalog["data_version"] = "1.0.0.151.22.0"
        self.assertEqual(updater.apply_local_sound_overrides(self.catalog, self.path), 2)

    def test_existing_target_pair_reused_and_occupied_group_safely_allocated(self):
        self.write_overrides()
        for stem, expected_id in (("Necklace", 47), ("Unrelated", 48)):
            with self.subTest(stem=stem):
                catalog = copy.deepcopy(self.catalog)
                catalog["sounds"].extend([sound(39, "AlreadyOccupied"), sound(47, stem)])
                occupied = copy.deepcopy(catalog["sounds"][1:])
                self.assertEqual(updater.apply_local_sound_overrides(catalog, self.path), 2)
                self.assertEqual([item["sound_group"] for item in catalog["items"]], [expected_id] * 2)
                self.assertEqual(catalog["sounds"][1:3], occupied)
                target = next(s for s in catalog["sounds"] if s["group_id"] == expected_id)
                self.assertEqual(target["pickup"], f"sounds/{expected_id}_pickup.ogg")
                self.assertEqual(len(catalog["sounds"]), 3 if stem == "Necklace" else 4)

    def test_missing_or_malformed_override_file_does_not_modify_catalog(self):
        before = copy.deepcopy(self.catalog)
        self.assertEqual(updater.apply_local_sound_overrides(self.catalog, self.path), 0)
        for value in ("{broken", "[]", '{"schema_version": 2}'):
            with self.subTest(value=value):
                self.path.write_text(value, encoding="utf-8")
                with self.assertWarns(RuntimeWarning):
                    self.assertEqual(updater.apply_local_sound_overrides(self.catalog, self.path), 0)
                self.assertEqual(self.catalog, before)

    def test_unsafe_paths_urls_and_unverified_audio_are_rejected(self):
        for key, value in (("pickup", "../outside.ogg"),
                           ("pickup_url", "http://abibuilder.com/audio/merchant/Item_Necklace_Up.ogg"),
                           ("pickup_url", "https://abibuilder.com.evil.test/audio/merchant/clip.ogg"),
                           ("pickup_url", "https://user@abibuilder.com/audio/merchant/clip.ogg"),
                           ("pickup_url", "https://abibuilder.com/audio/merchant/../clip.ogg"),
                           ("pickup_url", "https://cdn-abitracker.pages.dev/private/clip.ogg"),
                           ("pickup_url", "https://abibuilder.com/audio/merchant/clip.ogg\n"),
                           ("pickup_sha256", "not-a-hash")):
            with self.subTest(key=key, value=value):
                data = copy.deepcopy(self.overrides)
                data["sounds"][0][key] = value
                self.path.write_text(json.dumps(data), encoding="utf-8")
                before = copy.deepcopy(self.catalog)
                with self.assertWarns(RuntimeWarning):
                    self.assertEqual(updater.apply_local_sound_overrides(self.catalog, self.path), 0)
                self.assertEqual(self.catalog, before)

    def test_cdn_source_with_version_query_is_accepted(self):
        self.overrides["sounds"][0]["pickup_url"] = (
            "https://cdn-abitracker.pages.dev/data/merchant/audio/948946180-d15249feefb06625.ogg?v=1.0.0.151.17")
        self.write_overrides()
        self.assertEqual(updater.apply_local_sound_overrides(self.catalog, self.path), 2)

    def test_corrupt_cache_redownloaded_and_verified_before_replacement(self):
        entry = sound(39, "Necklace")
        path = self.output / entry["pickup"]
        path.write_bytes(b"OggS-corrupt")
        previous_urls = {entry["pickup"]: entry["pickup_url"]}
        with patch.object(updater, "download", return_value=AUDIO) as download:
            updater.fetch_audio_clip(entry, "pickup", self.output, previous_urls)
        download.assert_called_once()
        self.assertEqual(path.read_bytes(), AUDIO)
        with patch.object(updater, "download") as download:
            updater.fetch_audio_clip(entry, "pickup", self.output, previous_urls)
        download.assert_not_called()

    def test_hash_mismatch_keeps_existing_audio_and_does_not_accept_new_metadata(self):
        entry = sound(39, "Necklace")
        before = copy.deepcopy(entry)
        path = self.output / entry["pickup"]
        path.write_bytes(b"OggS-previous-cache")
        with patch.object(updater, "download", return_value=b"OggS-wrong-remote"):
            with self.assertRaisesRegex(ValueError, "verified SHA256"):
                updater.fetch_audio_clip(entry, "pickup", self.output, {}, refresh_audio=True)
        self.assertEqual(path.read_bytes(), b"OggS-previous-cache")
        self.assertEqual(entry, before)

    def test_offline_refresh_downloads_corrected_groups_and_reuses_them_next_time(self):
        self.write_overrides()
        (self.output / "catalog.json").write_text(json.dumps(self.catalog), encoding="utf-8")
        html_path = self.output / "public.html"
        html_path.write_text("fixture", encoding="utf-8")
        raw = {"version": "1.0.0.151.17",
               "items": [{"id": 408080029, "name": "Gem Necklace", "size": [1, 1],
                          "rarity": 4, "soundId": 33, "contactPrice": 4500}],
               "sounds": [{"id": 33, "pickup": "Item_GoldChain_Up", "drop": "Item_GoldChain_Down"}]}
        argv = ["download_catalog.py", "--output", str(self.output), "--html", str(html_path)]
        with patch.object(sys, "argv", argv), patch.object(updater, "extract_catalog", return_value=raw), \
                patch.object(updater, "download", return_value=AUDIO) as download, redirect_stdout(io.StringIO()):
            self.assertEqual(updater.main(), 0)
            self.assertEqual(download.call_count, 4)
            download.reset_mock()
            self.assertEqual(updater.main(), 0)
            download.assert_not_called()
        refreshed = json.loads((self.output / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(refreshed["items"][0]["sound_group"], 39)
        self.assertEqual(refreshed["items"][0]["name_ru"], "Ожерелье")
        self.assertEqual(refreshed["items"][0]["contact_price"], 4500)
        self.assertEqual((self.output / "sounds/39_pickup.ogg").read_bytes(), AUDIO)


if __name__ == "__main__":
    unittest.main()
