"""Background hotkeys through documented Windows Raw Input.

Only current down/up state for controls and assignable keys is retained in
memory, so remapping an already held key cannot look like a new press. This
module does not install hooks, read game processes, record text or key history,
or suppress game input.
"""
import ctypes
from ctypes import wintypes
import queue
import threading
from hotkey_settings import TOGGLE_KEYS, normalize_toggle_key, toggle_key_vk

user32 = ctypes.WinDLL('user32', use_last_error=True)
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [('usUsagePage', wintypes.USHORT), ('usUsage', wintypes.USHORT),
                ('dwFlags', wintypes.DWORD), ('hwndTarget', wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [('dwType', wintypes.DWORD), ('dwSize', wintypes.DWORD),
                ('hDevice', wintypes.HANDLE), ('wParam', wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [('MakeCode', wintypes.USHORT), ('Flags', wintypes.USHORT),
                ('Reserved', wintypes.USHORT), ('VKey', wintypes.USHORT),
                ('Message', wintypes.UINT), ('ExtraInformation', wintypes.ULONG)]


user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                  ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT

WM_INPUT = 0x00FF
WM_INPUT_DEVICE_CHANGE = 0x00FE
RIDEV_INPUTSINK = 0x00000100
RIDEV_DEVNOTIFY = 0x00002000
RIDEV_REMOVE = 0x00000001
RID_INPUT = 0x10000003
RI_KEY_BREAK = 1
MODIFIERS = frozenset((0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1,
                       0xA2, 0xA3, 0xA4, 0xA5))
_FIXED_ACTIONS = {0x75: 'edit', 0x76: 'next',
                  **{0x30 + n: n for n in range(10)}, **{0x60 + n: n for n in range(10)}}
_TRACKED_KEYS = frozenset(_FIXED_ACTIONS) | MODIFIERS | frozenset(toggle_key_vk(name) for name in TOGGLE_KEYS)
_listener_lock = threading.Lock()  # Windows allows one keyboard target per process.


class _KeyState:
    """Physical press edges, with modifier and per-device release tracking."""
    def __init__(self, toggle_key='F5'):
        self.active = False
        self._down = {}
        self.set_toggle_key(toggle_key)

    def set_toggle_key(self, name):
        canonical = normalize_toggle_key(name)
        replacement = dict(_FIXED_ACTIONS)
        replacement[toggle_key_vk(canonical)] = 'toggle'
        self.toggle_key = canonical
        self._actions = replacement
        # Keep down/up state, including keys that were not assigned before.
        # The user must release an already held key before it can activate.
        return canonical

    def remove_device(self, device):
        self._down = {key: value for key, value in self._down.items() if key[0] != device}

    def feed(self, device, vkey, make_code, flags):
        if make_code == 0xFF or vkey >= 0xFF:
            return None
        # Identify physical keys by scan code. A Num Lock change can change the
        # virtual key reported by a later break, without changing the key itself.
        key = (device, make_code or vkey, flags & 6, bool(make_code))
        if flags & RI_KEY_BREAK:
            self._down.pop(key, None)
            return None
        if vkey not in _TRACKED_KEYS:
            return None
        if key in self._down:
            return None
        self._down[key] = vkey
        if vkey in MODIFIERS or any(value in MODIFIERS for value in self._down.values()):
            return None
        action = self._actions.get(vkey)
        return action if action == 'toggle' or self.active else None


def overlay_style(widget, clickable=False):
    widget.update_idletasks()
    handle = user32.GetAncestor(widget.winfo_id(), 2) or widget.winfo_id()
    style = user32.GetWindowLongPtrW(handle, -20)
    style |= 0x00080000 | 0x00000080  # layered + tool window
    if clickable:
        style &= ~(0x00000020 | 0x08000000 | 0x00000080)
        style |= 0x00040000  # editable window can be selected with Alt+Tab
    else:
        style &= ~0x00040000
        style |= 0x00000020 | 0x08000000  # click-through + no activate
    user32.SetWindowLongPtrW(handle, -20, style)
    user32.SetWindowPos(handle, -1, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002 | 0x0020)
    user32.ShowWindow(handle, 4)


class Hotkeys:
    """Raw Input listener on its own native window and message-pump thread."""
    def __init__(self, toggle_key='F5'):
        self.toggle_key = normalize_toggle_key(toggle_key)
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self._closing = threading.Event()
        self._last_error = None
        self.thread = threading.Thread(target=self._run, name='Hotkeys', daemon=True)
        self.thread.start()

    def active(self, value):
        if not self._closing.is_set():
            self.commands.put(('active', bool(value)))

    def set_toggle_key(self, name):
        """Validate and acknowledge a mapping applied by the input thread."""
        canonical = normalize_toggle_key(name)
        if self._closing.is_set() or not self.thread.is_alive():
            raise RuntimeError('Обработчик клавиатуры уже остановлен')
        request = {'name': canonical, 'done': threading.Event(), 'lock': threading.Lock(),
                   'applied': False, 'cancelled': False}
        self.commands.put(('toggle_key', request))
        if not request['done'].wait(1):
            with request['lock']:
                if not request['applied']:
                    request['cancelled'] = True
                    raise RuntimeError('Обработчик клавиатуры не подтвердил смену клавиши')
        return canonical

    def close(self):
        self._closing.set()
        self.commands.put(('close', None))
        self.thread.join(1)

    def _error(self, description):
        if description != self._last_error:
            self._last_error = description
            self.events.put(('hotkey_error', description))

    def _keyboard_packet(self, handle):
        size = wintypes.UINT()
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        result = user32.GetRawInputData(handle, RID_INPUT, None, ctypes.byref(size), header_size)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if size.value < header_size or size.value > 65536:
            raise ValueError('Некорректный размер пакета клавиатуры Windows')
        data = ctypes.create_string_buffer(size.value)
        copied = user32.GetRawInputData(handle, RID_INPUT, data, ctypes.byref(size), header_size)
        if copied == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if copied < header_size or copied > len(data):
            raise ValueError('Неполный пакет клавиатуры Windows')
        header = RAWINPUTHEADER.from_buffer_copy(data)
        if header.dwType != 1:
            return None
        minimum = header_size + ctypes.sizeof(RAWKEYBOARD)
        if copied < minimum or not minimum <= header.dwSize <= copied:
            raise ValueError('Неполный пакет клавиатуры Windows')
        keyboard = RAWKEYBOARD.from_buffer_copy(data, header_size)
        return (header.hDevice or 0, keyboard.VKey, keyboard.MakeCode, keyboard.Flags)

    def _dispatch_input(self, message, state, emit_actions=True):
        try:
            if message.message == WM_INPUT:
                packet = self._keyboard_packet(message.lParam)
                if packet is not None:
                    action = state.feed(*packet)
                    if emit_actions and action is not None:
                        self.events.put(('key', action))
            elif message.message == WM_INPUT_DEVICE_CHANGE and message.wParam == 2:
                state.remove_device(message.lParam)
        except Exception as exc:
            self._error(f'Ошибка чтения клавиш Windows: {exc}')
        finally:
            # STATIC's default procedure performs Windows packet cleanup.
            user32.DispatchMessageW(ctypes.byref(message))

    def _remap(self, state, request):
        # Drain already queued make/break packets with the old mapping before
        # assigning a key. Otherwise a queued make from an already held key
        # could be mistaken for a fresh press under the new mapping.
        message = wintypes.MSG()
        while not self._closing.is_set() and user32.PeekMessageW(
                ctypes.byref(message), None, WM_INPUT_DEVICE_CHANGE, WM_INPUT, 1):
            self._dispatch_input(message, state, emit_actions=False)
        with request['lock']:
            if not request['cancelled'] and not self._closing.is_set():
                state.set_toggle_key(request['name'])
                self.toggle_key = request['name']
                request['applied'] = True
                request['done'].set()

    def _run(self):
        hwnd, registered, owns_listener = None, False, False
        state = _KeyState(self.toggle_key)
        try:
            if self._closing.is_set():
                return
            owns_listener = _listener_lock.acquire(blocking=False)
            if not owns_listener:
                raise RuntimeError('В этом процессе уже работает обработчик клавиатуры')
            # The built-in STATIC class provides default dispatch/cleanup. The
            # message-only parent keeps this listener invisible and unfocusable.
            hwnd = user32.CreateWindowExW(0, 'STATIC', 'ABI Sound Scout Input', 0,
                                         0, 0, 0, 0, wintypes.HWND(-3), None, None, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            device = RAWINPUTDEVICE(1, 6, RIDEV_INPUTSINK | RIDEV_DEVNOTIFY, hwnd)
            if not user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            self.events.put(('hotkey_ready', 'Фоновые клавиши подключены'))
            message = wintypes.MSG()
            while not self._closing.is_set():
                try:
                    command, value = self.commands.get(timeout=.01)
                    while True:
                        if command == 'close':
                            self._closing.set()
                            break
                        if command == 'active':
                            # Keep held physical keys across enable/disable;
                            # changing mode is not a new key press.
                            state.active = value
                        elif command == 'toggle_key':
                            self._remap(state, value)
                        command, value = self.commands.get_nowait()
                except queue.Empty:
                    pass
                for _ in range(256):
                    if self._closing.is_set() or not user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                        break
                    if message.message == 0x0012:  # WM_QUIT
                        self._closing.set()
                        break
                    self._dispatch_input(message, state)
        except Exception as exc:
            self._error(f'Не удалось подключить фоновые клавиши Windows: {exc}')
        finally:
            if registered:
                remove = RAWINPUTDEVICE(1, 6, RIDEV_REMOVE, None)
                if not user32.RegisterRawInputDevices(ctypes.byref(remove), 1, ctypes.sizeof(remove)):
                    self._error(f'Ошибка отключения клавиш Windows: {ctypes.WinError(ctypes.get_last_error())}')
            if hwnd:
                user32.DestroyWindow(hwnd)
            if owns_listener:
                _listener_lock.release()
