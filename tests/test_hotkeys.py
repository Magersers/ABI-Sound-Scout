"""Raw Input behavior without real key injections or game interaction."""
import ctypes
import importlib.util
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeUser32:
    def __init__(self, fail_register=False):
        self.fail_register = fail_register
        self.created, self.registered, self.pending = {}, {}, {}
        self.destroyed, self.unregistered, self.dispatched = [], [], []
        self.packets = {}
        self.CreateWindowExW = Function(self.create)
        self.DestroyWindow = Function(self.destroy)
        self.RegisterRawInputDevices = Function(self.register)
        self.PeekMessageW = Function(self.peek)
        self.DispatchMessageW = Function(self.dispatch)
        self.GetRawInputData = Function(self.read)
        for name in ('GetAncestor', 'GetWindowLongPtrW', 'SetWindowLongPtrW', 'SetWindowPos', 'ShowWindow'):
            setattr(self, name, Function(lambda *args: 1))

    def create(self, *args):
        owner = threading.get_ident()
        hwnd = len(self.created) + 100
        self.created[owner] = hwnd
        self.pending[owner] = queue.Queue()
        return hwnd

    def destroy(self, hwnd):
        self.destroyed.append((threading.get_ident(), hwnd))
        return 1

    def register(self, pointer, count, size):
        device = pointer._obj
        owner = threading.get_ident()
        if device.dwFlags == 1:
            assert not device.hwndTarget
            self.unregistered.append((owner, self.registered.pop(owner)))
            return 1
        if self.fail_register:
            ctypes.set_last_error(5)
            return 0
        self.registered[owner] = (device.usUsagePage, device.usUsage, device.dwFlags, device.hwndTarget, size)
        return 1

    def peek(self, pointer, *args):
        owner = threading.get_ident()
        try:
            message, wparam, lparam = self.pending[owner].get_nowait()
        except queue.Empty:
            return 0
        pointer._obj.hwnd = self.created[owner]
        pointer._obj.message = message
        pointer._obj.wParam = wparam
        pointer._obj.lParam = lparam
        return 1

    def dispatch(self, pointer):
        self.dispatched.append((pointer._obj.message, pointer._obj.wParam, pointer._obj.lParam))
        return 0

    def read(self, handle, command, data, size, header_size):
        packet = self.packets[int(handle)]
        size._obj.value = len(packet)
        if data is None:
            return 0
        ctypes.memmove(data, packet, len(packet))
        return len(packet)

    def post_packet(self, listener, packet, foreground=False):
        handle = len(self.packets) + 1000
        self.packets[handle] = packet
        self.pending[listener.thread.ident].put((0x00FF, 0 if foreground else 1, handle))

    def key(self, module, listener, vkey, scan, flags=0, device=7, foreground=False):
        size = ctypes.sizeof(module.RAWINPUTHEADER) + ctypes.sizeof(module.RAWKEYBOARD)
        header = module.RAWINPUTHEADER(1, size, device, 0 if foreground else 1)
        keyboard = module.RAWKEYBOARD(scan, flags, 0, vkey, 0x101 if flags & 1 else 0x100, 0)
        self.post_packet(listener, bytes(header) + bytes(keyboard), foreground)


def load_with(fake):
    source = Path(__file__).resolve().parents[1] / 'winhotkeys.py'
    spec = importlib.util.spec_from_file_location('review_winhotkeys', source)
    module = importlib.util.module_from_spec(spec)
    with patch.object(ctypes, 'WinDLL', return_value=fake):
        spec.loader.exec_module(module)
    return module


def wait_until(condition, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.005)
    return condition()


class KeyStateTests(unittest.TestCase):
    def setUp(self):
        self.module = load_with(FakeUser32())
        self.state = self.module._KeyState()

    def test_hold_repeat_release_press(self):
        self.assertEqual(self.state.feed(1, 0x74, 0x3F, 0), 'toggle')
        self.assertIsNone(self.state.feed(1, 0x74, 0x3F, 0))
        self.assertIsNone(self.state.feed(1, 0x74, 0x3F, 1))
        self.assertEqual(self.state.feed(1, 0x74, 0x3F, 0), 'toggle')

    def test_inactive_keys_stay_silent_when_enabled_while_held(self):
        for vkey, scan in ((0x36, 7), (0x66, 0x4D), (0x75, 0x40), (0x76, 0x41)):
            state = self.module._KeyState()
            self.assertIsNone(state.feed(1, vkey, scan, 0))
            state.active = True
            self.assertIsNone(state.feed(1, vkey, scan, 0))
            state.feed(1, vkey, scan, 1)
            self.assertIsNotNone(state.feed(1, vkey, scan, 0))

    def test_zero_and_numpad_digits(self):
        self.state.active = True
        self.assertEqual(self.state.feed(1, 0x30, 0x0B, 0), 0)
        self.assertEqual(self.state.feed(1, 0x69, 0x49, 0), 9)

    def test_modifiers_and_release_do_not_turn_repeat_into_press(self):
        for modifier, scan in ((0x11, 0x1D), (0x12, 0x38), (0x10, 0x2A), (0x5B, 0x5B)):
            state = self.module._KeyState()
            state.active = True
            state.feed(1, modifier, scan, 0)
            self.assertIsNone(state.feed(1, 0x35, 6, 0))
            state.feed(1, modifier, scan, 1)
            self.assertIsNone(state.feed(1, 0x35, 6, 0))
            state.feed(1, 0x35, 6, 1)
            self.assertEqual(state.feed(1, 0x35, 6, 0), 5)

    def test_device_removal_only_releases_that_device(self):
        self.state.feed(1, 0x11, 0x1D, 0)
        self.state.feed(2, 0x11, 0x1D, 0)
        self.state.remove_device(1)
        self.assertIsNone(self.state.feed(3, 0x74, 0x3F, 0))
        self.state.feed(3, 0x74, 0x3F, 1)
        self.state.remove_device(2)
        self.assertEqual(self.state.feed(3, 0x74, 0x3F, 0), 'toggle')

    def test_numlock_change_still_releases_physical_key(self):
        self.state.active = True
        self.assertEqual(self.state.feed(1, 0x60, 0x52, 0), 0)
        self.state.feed(1, 0x2D, 0x52, 1)
        self.assertEqual(self.state.feed(1, 0x60, 0x52, 0), 0)

    def test_unassignable_keys_and_overruns_are_not_retained(self):
        for vkey in (0x08, 0x09, 0x0D, 0x1B, 0x20, 0xBA, 0xBB, 0xDE):
            self.assertIsNone(self.state.feed(1, vkey, vkey, 0))
        self.assertIsNone(self.state.feed(1, 0x74, 0xFF, 0))
        self.assertIsNone(self.state.feed(1, 0xFF, 1, 0))
        self.assertEqual(self.state._down, {})

    def test_custom_key_works_while_inactive_and_disables_old_default(self):
        state = self.module._KeyState('h')
        self.assertEqual(state.toggle_key, 'H')
        self.assertIsNone(state.feed(1, 0x74, 0x3F, 0))
        self.assertEqual(state.feed(1, 0x48, 0x23, 0), 'toggle')
        self.assertIsNone(state.feed(1, 0x48, 0x23, 0))
        state.feed(1, 0x48, 0x23, 1)
        self.assertEqual(state.feed(1, 0x48, 0x23, 0), 'toggle')

    def test_supported_function_letter_and_navigation_keys(self):
        for name, vk, scan, flags in [('F1', 0x70, 0x3B, 0), ('F12', 0x7B, 0x58, 0),
                                      ('A', 0x41, 0x1E, 0), ('Z', 0x5A, 0x2C, 0),
                                      ('Insert', 0x2D, 0x52, 2), ('Home', 0x24, 0x47, 2),
                                      ('End', 0x23, 0x4F, 2), ('PageUp', 0x21, 0x49, 2),
                                      ('PageDown', 0x22, 0x51, 2)]:
            with self.subTest(name=name):
                state = self.module._KeyState(name)
                self.assertEqual(state.feed(1, vk, scan, flags), 'toggle')

    def test_remap_replaces_old_action_without_arming_held_new_key(self):
        self.assertIsNone(self.state.feed(1, 0x48, 0x23, 0))
        self.assertEqual(self.state.set_toggle_key('H'), 'H')
        self.assertIsNone(self.state.feed(1, 0x48, 0x23, 0))
        self.assertIsNone(self.state.feed(1, 0x74, 0x3F, 0))
        self.state.feed(1, 0x48, 0x23, 1)
        self.assertEqual(self.state.feed(1, 0x48, 0x23, 0), 'toggle')
        self.state.set_toggle_key('F5')
        self.assertIsNone(self.state.feed(1, 0x74, 0x3F, 0))
        self.state.feed(1, 0x74, 0x3F, 1)
        self.assertEqual(self.state.feed(1, 0x74, 0x3F, 0), 'toggle')

    def test_reserved_or_unknown_remap_does_not_change_existing_mapping(self):
        for name in ('0', '1', '9', 'F6', 'F7', 'Space', 'Ctrl+H', '', 'F13'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.state.set_toggle_key(name)
                self.assertEqual(self.state.toggle_key, 'F5')
        self.assertEqual(self.state.feed(1, 0x74, 0x3F, 0), 'toggle')

    def test_remapping_does_not_change_fixed_controls(self):
        self.state.set_toggle_key('H')
        self.state.active = True
        for vk, scan, expected in ((0x75, 0x40, 'edit'), (0x76, 0x41, 'next'),
                                   (0x30, 0x0B, 0), (0x69, 0x49, 9)):
            self.assertEqual(self.state.feed(1, vk, scan, 0), expected)


class HotkeyTests(unittest.TestCase):
    def make_listener(self, fake=None, toggle_key='F5'):
        fake = fake or FakeUser32()
        module = load_with(fake)
        listener = module.Hotkeys(toggle_key=toggle_key)
        self.addCleanup(listener.close)
        self.assertEqual(listener.events.get(timeout=1)[0], 'hotkey_ready')
        return fake, module, listener

    def test_background_input_and_own_thread_cleanup(self):
        fake, module, listener = self.make_listener()
        owner = listener.thread.ident
        self.assertNotEqual(owner, threading.get_ident())
        self.assertEqual(fake.registered[owner][:3], (1, 6, 0x2100))
        fake.key(module, listener, 0x74, 0x3F)
        self.assertEqual(listener.events.get(timeout=1), ('key', 'toggle'))
        listener.active(True)
        fake.key(module, listener, 0x36, 7)
        self.assertEqual(listener.events.get(timeout=1), ('key', 6))
        listener.close()
        self.assertFalse(listener.thread.is_alive())
        self.assertEqual(fake.registered, {})
        self.assertEqual(fake.destroyed, [(owner, fake.created[owner])])
        self.assertEqual(fake.unregistered[0][0], owner)

    def test_foreground_packets_are_dispatched_for_windows_cleanup(self):
        fake, module, listener = self.make_listener()
        fake.key(module, listener, 0x74, 0x3F, foreground=True)
        self.assertEqual(listener.events.get(timeout=1), ('key', 'toggle'))
        self.assertTrue(wait_until(lambda: len(fake.dispatched) == 1))
        self.assertEqual(fake.dispatched[0][:2], (0x00FF, 0))

    def test_device_removal_clears_held_key(self):
        fake, module, listener = self.make_listener()
        fake.key(module, listener, 0x74, 0x3F, device=17)
        self.assertEqual(listener.events.get(timeout=1), ('key', 'toggle'))
        fake.pending[listener.thread.ident].put((0x00FE, 2, 17))
        fake.key(module, listener, 0x74, 0x3F, device=17)
        self.assertEqual(listener.events.get(timeout=1), ('key', 'toggle'))

    def test_registration_failure_reports_win32_error_and_destroys_window(self):
        fake = FakeUser32(fail_register=True)
        listener = load_with(fake).Hotkeys()
        try:
            kind, description = listener.events.get(timeout=1)
            self.assertEqual(kind, 'hotkey_error')
            self.assertIn('5', description)
        finally:
            listener.close()
        self.assertEqual(fake.registered, {})
        self.assertEqual(len(fake.destroyed), 1)

    def test_malformed_packet_reports_error_and_still_dispatches(self):
        fake, module, listener = self.make_listener()
        fake.post_packet(listener, b'bad')
        self.assertEqual(listener.events.get(timeout=1)[0], 'hotkey_error')
        self.assertTrue(wait_until(lambda: len(fake.dispatched) == 1))
        fake.key(module, listener, 0x74, 0x3F)
        self.assertEqual(listener.events.get(timeout=1), ('key', 'toggle'))

    def test_second_listener_cannot_replace_or_remove_first_in_process(self):
        fake, module, first = self.make_listener()
        second = module.Hotkeys()
        try:
            self.assertEqual(second.events.get(timeout=1)[0], 'hotkey_error')
        finally:
            second.close()
        self.assertIn(first.thread.ident, fake.registered)
        first.close()
        replacement = module.Hotkeys()
        try:
            self.assertEqual(replacement.events.get(timeout=1)[0], 'hotkey_ready')
        finally:
            replacement.close()

    def test_close_aborts_queued_input_and_is_idempotent(self):
        fake, module, listener = self.make_listener()
        for _ in range(100):
            listener.active(True)
            listener.active(False)
        listener.close()
        listener.close()
        self.assertFalse(listener.thread.is_alive())
        self.assertEqual(fake.registered, {})

    def test_configured_key_and_runtime_remap_are_applied_on_worker(self):
        fake, module, listener = self.make_listener(toggle_key='H')
        fake.key(module, listener, 0x48, 0x23)
        self.assertEqual(listener.events.get(timeout=1), ('key', 'toggle'))
        fake.key(module, listener, 0x48, 0x23, flags=1)
        self.assertEqual(listener.set_toggle_key('Insert'), 'Insert')
        fake.key(module, listener, 0x48, 0x23)
        fake.key(module, listener, 0x74, 0x3F)
        fake.key(module, listener, 0x2D, 0x52, flags=2)
        self.assertEqual(listener.events.get(timeout=1), ('key', 'toggle'))
        self.assertTrue(wait_until(lambda: len(fake.dispatched) == 5))
        self.assertTrue(listener.events.empty())
        self.assertEqual(len(fake.registered), 1)

    def test_invalid_constructor_does_not_start_native_listener(self):
        fake = FakeUser32()
        module = load_with(fake)
        with self.assertRaises(ValueError):
            module.Hotkeys(toggle_key='F6')
        self.assertEqual(fake.created, {})

    def test_invalid_and_closed_runtime_remaps_report_synchronously(self):
        fake, module, listener = self.make_listener()
        with self.assertRaises(ValueError):
            listener.set_toggle_key('7')
        self.assertEqual(listener.toggle_key, 'F5')
        listener.close()
        with self.assertRaises(RuntimeError):
            listener.set_toggle_key('H')

    def test_queued_make_before_remap_is_not_a_new_press(self):
        fake = FakeUser32()
        module = load_with(fake)
        listener = module.Hotkeys.__new__(module.Hotkeys)
        listener.events = queue.Queue()
        listener._closing = threading.Event()
        listener._last_error = None
        listener.toggle_key = 'F5'
        listener.thread = SimpleNamespace(ident=threading.get_ident())
        fake.create()
        state = module._KeyState()
        fake.key(module, listener, 0x48, 0x23)
        request = {'name': 'H', 'done': threading.Event(), 'lock': threading.Lock(),
                   'applied': False, 'cancelled': False}
        listener._remap(state, request)
        self.assertTrue(request['applied'])
        self.assertTrue(request['done'].is_set())
        self.assertEqual(listener.toggle_key, 'H')
        self.assertTrue(listener.events.empty())
        self.assertEqual(len(fake.dispatched), 1)
        self.assertIsNone(state.feed(7, 0x48, 0x23, 0))
        state.feed(7, 0x48, 0x23, 1)
        self.assertEqual(state.feed(7, 0x48, 0x23, 0), 'toggle')

    def test_cancelled_remap_cannot_apply_later(self):
        fake = FakeUser32()
        module = load_with(fake)
        listener = module.Hotkeys.__new__(module.Hotkeys)
        listener._closing = threading.Event()
        listener.toggle_key = 'F5'
        fake.create()
        state = module._KeyState()
        request = {'name': 'H', 'done': threading.Event(), 'lock': threading.Lock(),
                   'applied': False, 'cancelled': True}
        listener._remap(state, request)
        self.assertFalse(request['applied'])
        self.assertFalse(request['done'].is_set())
        self.assertEqual(listener.toggle_key, 'F5')
        self.assertEqual(state.toggle_key, 'F5')


if __name__ == '__main__':
    unittest.main()
