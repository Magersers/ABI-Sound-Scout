"""Review smoke checks with no Tk window, audio capture or real hotkeys."""
import base64
from pathlib import Path
import queue
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class AppReviewTests(unittest.TestCase):
    def test_catalog_name_is_only_data_in_speech_script(self):
        malicious_name = "item’); Write-Output SCOUT_INJECTION_PROBE; # ‘ ' $() `"
        scout = app.Scout.__new__(app.Scout)
        scout.catalog = {'items': [dict(id=1, name=malicious_name, sound_group=1,
                                         width=1, height=1, contact_price=10)]}
        scout.voice = SimpleNamespace(get=lambda: True)
        scout.selected, scout.token = 0, 1
        scout.speaking, scout.spoken_signature, scout.last_spoken = False, None, -1e9
        scout.service = SimpleNamespace(events=queue.Queue())
        with patch.object(app.threading, 'Thread', ImmediateThread), patch.object(app.subprocess, 'run') as run:
            scout.speak([dict(group_id=1, score=.99)])
        script = base64.b64decode(run.call_args.args[0][-1]).decode('utf-16-le')
        self.assertNotIn('SCOUT_INJECTION_PROBE', script)
        encoded_text = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script).group(1)
        self.assertIn(malicious_name, base64.b64decode(encoded_text).decode('utf-8'))
        self.assertIn('Speak($scoutText)', script)
        self.assertFalse(scout.speaking)

    def make_scout_for_result(self, group):
        scout = app.Scout.__new__(app.Scout)
        scout.closing, scout.active = False, True
        scout.token, scout.page = 1, 2
        scout.results = [dict(group_id=22, score=.8)]
        scout.hotkeys = SimpleNamespace(events=queue.Queue())
        scout.service = SimpleNamespace(events=queue.Queue())
        scout.service.events.put(('result', 1, ([dict(group_id=group, score=.9)], .1)))
        scout.level = SimpleNamespace(delete=Mock(), create_rectangle=Mock(), winfo_width=lambda: 100)
        scout.overlay_title = SimpleNamespace(config=Mock())
        scout.render_results, scout.speak = Mock(), Mock()
        scout.root = SimpleNamespace(after=Mock())
        return scout

    def test_similarity_drift_preserves_page(self):
        scout = self.make_scout_for_result(22)
        scout.poll()
        self.assertEqual(scout.page, 2)
        scout.render_results.assert_called_once()

    def test_new_sound_group_resets_page(self):
        scout = self.make_scout_for_result(18)
        scout.poll()
        self.assertEqual(scout.page, 0)

    def test_audio_ready_does_not_erase_keyboard_error(self):
        scout = app.Scout.__new__(app.Scout)
        scout.closing, scout.active, scout.token = False, False, 1
        scout.hotkeys = SimpleNamespace(events=queue.Queue())
        scout.service = SimpleNamespace(events=queue.Queue())
        scout.keyboard_text, scout.state_text = Mock(), Mock()
        scout.root = SimpleNamespace(after=Mock())
        scout.hotkeys.events.put(('hotkey_error', 'Win32 5'))
        scout.service.events.put(('ready', 0, 76))
        scout.poll()
        scout.keyboard_text.set.assert_called_once_with('Клавиатура: Win32 5')
        self.assertTrue(scout.ready)

    def test_background_toggle_is_reported_and_dispatched_once(self):
        scout = app.Scout.__new__(app.Scout)
        scout.closing, scout.active, scout.token = False, False, 1
        scout.hotkeys = SimpleNamespace(events=queue.Queue())
        scout.service = SimpleNamespace(events=queue.Queue())
        scout.keyboard_text = Mock()
        scout.root = SimpleNamespace(after=Mock())
        scout.overlay = SimpleNamespace(state=lambda: 'withdrawn')
        scout.toggle = Mock()
        scout.hotkeys.events.put(('key', 'toggle'))
        scout.poll()
        scout.toggle.assert_called_once()
        self.assertIn('принято F5', scout.keyboard_text.set.call_args.args[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)

