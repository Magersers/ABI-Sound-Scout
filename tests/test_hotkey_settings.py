"""Opening-key preferences and UI changes without windows or input listeners."""
import json
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hotkey_settings as settings
import app


class HotkeySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ABI Sound Scout' / 'settings.json'

    def test_all_selectable_keys_have_unique_vkeys_and_fixed_controls_are_excluded(self):
        self.assertEqual(len(settings.TOGGLE_KEYS), 41)
        virtual_keys = [settings.toggle_key_vk(name) for name in settings.TOGGLE_KEYS]
        self.assertEqual(len(set(virtual_keys)), len(virtual_keys))
        self.assertNotIn(0x75, virtual_keys)  # F6: editable overlay
        self.assertNotIn(0x76, virtual_keys)  # F7: pagination
        self.assertFalse(set(range(0x30, 0x3A)) & set(virtual_keys))
        self.assertFalse(set(range(0x60, 0x6A)) & set(virtual_keys))

    def test_canonical_names_and_physical_letter_virtual_keys(self):
        for name, canonical, vkey in ((' f12 ', 'F12', 0x7B), ('a', 'A', 0x41),
                                      ('z', 'Z', 0x5A), ('PAGEUP', 'PageUp', 0x21),
                                      ('insert', 'Insert', 0x2D), ('Home', 'Home', 0x24)):
            with self.subTest(name=name):
                self.assertEqual(settings.normalize_toggle_key(name), canonical)
                self.assertEqual(settings.toggle_key_vk(name), vkey)

    def test_invalid_and_reserved_values_are_rejected_without_aliasing(self):
        for key in ('F6', 'F7', '1', '0', 'Ctrl+A', 'А', 'F13', '', None, 65):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    settings.normalize_toggle_key(key)

    def test_missing_corrupt_or_invalid_settings_use_f5_without_rewriting_file(self):
        self.assertEqual(settings.load_toggle_key(self.path), 'F5')
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir()
        for content in ('{broken', '[]', '{"toggle_key":"F6"}', '{"toggle_key":null}', '{}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding='utf-8')
                self.assertEqual(settings.load_toggle_key(self.path), 'F5')
                self.assertEqual(self.path.read_text(encoding='utf-8'), content)

    def test_save_canonical_preference_only_and_load_from_local_app_data(self):
        with patch.dict(settings.os.environ, {'LOCALAPPDATA': self.temp.name}):
            settings.save_toggle_key(' pagedown ')
            self.assertEqual(settings.settings_path(), self.path)
            self.assertEqual(settings.load_toggle_key(), 'PageDown')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {'toggle_key': 'PageDown'})
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_failed_atomic_replace_preserves_previous_setting_and_cleans_tempfile(self):
        settings.save_toggle_key('Insert', self.path)
        before = self.path.read_bytes()
        with patch.object(settings.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                settings.save_toggle_key('F9', self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])


class AppHotkeySettingTests(unittest.TestCase):
    def make_scout(self):
        scout = app.Scout.__new__(app.Scout)
        scout.active, scout.editing = False, False
        scout.toggle_key = 'F5'
        scout.toggle_choice = SimpleNamespace(get=lambda: 'Insert', set=Mock())
        scout.hotkeys = SimpleNamespace(set_toggle_key=Mock())
        scout.state_text = Mock()
        scout.toggle_btn, scout.toggle_help, scout.overlay_hint = Mock(), Mock(), Mock()
        return scout

    def test_changed_key_applies_and_saves_without_restarting_capture(self):
        scout = self.make_scout()
        scout.active, scout.editing = True, True
        with patch.object(app, 'save_toggle_key') as save:
            scout.change_toggle_key()
        scout.hotkeys.set_toggle_key.assert_called_once_with('Insert')
        save.assert_called_once_with('Insert')
        self.assertEqual(scout.toggle_key, 'Insert')
        self.assertTrue(scout.active)
        self.assertEqual(scout.toggle_btn.config.call_args.kwargs['text'], 'Insert   Остановить')
        self.assertIn('Insert', scout.toggle_help.config.call_args.kwargs['text'])
        self.assertEqual(scout.overlay_hint.config.call_args.kwargs['text'], 'Insert скрыть  ·  F6 пропускать мышь')

    def test_backend_rejection_restores_choice_and_does_not_save(self):
        scout = self.make_scout()
        scout.hotkeys.set_toggle_key.side_effect = RuntimeError('closed')
        with patch.object(app, 'save_toggle_key') as save:
            scout.change_toggle_key()
        save.assert_not_called()
        self.assertEqual(scout.toggle_key, 'F5')
        scout.toggle_choice.set.assert_called_once_with('F5')
        self.assertIn('Не удалось изменить', scout.state_text.set.call_args.args[0])

    def test_failed_save_keeps_working_key_and_explains_session_only_setting(self):
        scout = self.make_scout()
        with patch.object(app, 'save_toggle_key', side_effect=PermissionError('read only')):
            scout.change_toggle_key()
        self.assertEqual(scout.toggle_key, 'Insert')
        scout.toggle_choice.set.assert_called_once_with('Insert')
        self.assertIn('до закрытия программы', scout.state_text.set.call_args.args[0])
        self.assertIn('не удалось', scout.state_text.set.call_args.args[0])

    def test_poll_reports_configured_key_and_ready_hint(self):
        scout = self.make_scout()
        scout.toggle_key = 'PageDown'
        scout.closing, scout.token = False, 1
        scout.hotkeys.events = queue.Queue()
        scout.hotkeys.events.put(('key', 'toggle'))
        scout.service = SimpleNamespace(events=queue.Queue())
        scout.service.events.put(('ready', 0, 156))
        scout.keyboard_text = Mock()
        scout.root = SimpleNamespace(after=Mock())
        scout.overlay = SimpleNamespace(state=lambda: 'withdrawn')
        scout.toggle = Mock()
        scout.poll()
        scout.toggle.assert_called_once()
        self.assertIn('принято PageDown', scout.keyboard_text.set.call_args.args[0])
        self.assertIn('нажмите PageDown', scout.state_text.set.call_args.args[0])

    def test_self_test_entrypoint_runs_before_tk_or_instance_creation(self):
        runner = Mock(return_value=0)
        with patch.dict(sys.modules, {'selftest': SimpleNamespace(run=runner)}), \
                patch.object(sys, 'argv', ['scout.exe', '--self-test', 'report.json']), \
                patch.object(app.tk, 'Tk') as window, patch.object(app, 'SingleInstance') as instance:
            with self.assertRaises(SystemExit) as exit:
                app.main()
        self.assertEqual(exit.exception.code, 0)
        runner.assert_called_once_with(Path('report.json'))
        window.assert_not_called()
        instance.assert_not_called()


if __name__ == '__main__':
    unittest.main()
