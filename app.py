"""ABI Sound Scout — standalone Windows desktop helper."""
from __future__ import annotations
import base64
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from domain import load_catalog, candidates, money, SHAPES
from winhotkeys import Hotkeys, overlay_style
from instance import SingleInstance
from hotkey_settings import TOGGLE_KEYS, load_toggle_key, save_toggle_key, normalize_toggle_key

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
VERSION = '0.4.0'
BG, PANEL, CARD = '#101722', '#182231', '#202d3e'
FG, MUTED, ACCENT, GOLD = '#eef4fb', '#96a8bd', '#6ce0bf', '#edc37b'
RARITY = {1: '#a7b5c6', 3: '#77b9f1', 4: '#bd9af1', 5: '#ecc36f', 6: '#fb8988'}


def label(parent, text='', size=10, color=FG, **kw):
    return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color,
                    font=('Segoe UI', size), **kw)


def button(parent, text, command, primary=False, **kw):
    kw.setdefault('padx', 16)
    kw.setdefault('pady', 10)
    return tk.Button(parent, text=text, command=command, bg=ACCENT if primary else CARD,
                     fg=BG if primary else FG, activebackground='#a0efd7', activeforeground=BG,
                     relief='flat', bd=0, cursor='hand2',
                     font=('Segoe UI', 10, 'bold'), **kw)


class AudioService:
    """All capture and matching are serialized on one worker thread."""
    def __init__(self, references):
        self.commands, self.events = queue.Queue(), queue.Queue()
        self.thread = threading.Thread(target=self.run, args=(references,), daemon=True)
        self.thread.start()

    def run(self, references):
        capture, token = None, 0
        try:
            from engine import Matcher, LoopbackCapture
            matcher = Matcher(references)
            if matcher.errors or not matcher.references:
                raise RuntimeError('\n'.join(matcher.errors) or 'Нет эталонов')
            self.events.put(('ready', 0, len(matcher.references)))
            while True:
                try:
                    command, token_arg, value = self.commands.get(timeout=.32 if capture else .15)
                    token = token_arg
                    if capture:
                        capture.stop()
                        capture = None
                    if command == 'quit':
                        return
                    if command == 'start':
                        try:
                            capture = LoopbackCapture(value)
                            capture.start()
                            self.events.put(('listening', token, capture.device_name))
                        except Exception as exc:
                            capture = None
                            self.events.put(('error', token, str(exc)))
                    elif command == 'file':
                        try:
                            import soundfile as sf
                            with sf.SoundFile(value) as stream:
                                if len(stream) / stream.samplerate > 30:
                                    raise ValueError('Выберите запись длительностью до 30 секунд.')
                                samples = stream.read(dtype='float32')
                                rate = stream.samplerate
                            result = matcher.match(samples, rate)
                            self.events.put(('file_result', token, result))
                        except Exception as exc:
                            self.events.put(('error', token, str(exc)))
                except queue.Empty:
                    pass
                if capture:
                    try:
                        if capture.error or not capture.running:
                            raise RuntimeError(capture.error or 'Устройство воспроизведения отключено.')
                        samples, rate, level = capture.snapshot(3.0)
                        results = matcher.match(samples, rate) if len(samples) else []
                        if results:
                            newest = max(r['end_seconds'] for r in results)
                            results = [r for r in results if r['end_seconds'] >= newest - .16]
                        self.events.put(('result', token, (results, level)))
                    except Exception as exc:
                        capture.stop()
                        capture = None
                        self.events.put(('error', token, str(exc)))
        except Exception as exc:
            logging.exception('Audio worker')
            self.events.put(('fatal', token, str(exc)))
        finally:
            if capture:
                capture.stop()


class Scout:
    toggle_key = 'F5'

    def __init__(self, root):
        self.root = root
        self.catalog, self.references = load_catalog(BASE / 'data')
        self.active, self.editing, self.ready = False, False, False
        self.analyzing = False
        self.token, self.selected, self.page = 0, 0, 0
        self.results, self.devices = [], []
        self.last_found, self.last_spoken = 0.0, 0.0
        self.spoken_signature = None
        self.speaking = False
        self.closing = False
        self.toggle_key = load_toggle_key()
        self.toggle_choice = tk.StringVar(value=self.toggle_key)
        self.voice = tk.BooleanVar(value=False)
        self.state_text = tk.StringVar(value='Загрузка звуковых эталонов…')
        self.keyboard_text = tk.StringVar(value='Клавиатура: подключение…')
        self.setup_window()
        self.setup_overlay()
        self.hotkeys = Hotkeys(toggle_key=self.toggle_key)
        self.service = AudioService(self.references)
        self.refresh_devices()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(50, self.poll)

    def setup_window(self):
        r = self.root
        r.title(f'ABI Sound Scout {VERSION}')
        if (BASE / 'scout.ico').is_file():
            r.iconbitmap(str(BASE / 'scout.ico'))
        r.configure(bg=BG)
        r.geometry('820x720')
        r.minsize(780, 720)
        r.option_add('*Font', ('Segoe UI', 10))
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TCombobox', fieldbackground=CARD, background=CARD, foreground=FG,
                        arrowcolor=FG, padding=8)
        style.map('TCombobox', fieldbackground=[('readonly', CARD)], foreground=[('readonly', FG)])
        wrap = tk.Frame(r, bg=BG, padx=30, pady=23)
        wrap.pack(fill='both', expand=True)
        label(wrap, 'ABI  /  SOUND SCOUT', 11, ACCENT).pack(anchor='w')
        label(wrap, 'Услышь. Сузь выбор.', 26).pack(anchor='w', pady=(10, 4))
        label(wrap, 'Скрытый груз полевого торговца · Arena Breakout: Infinite', 11, MUTED).pack(anchor='w')
        stats = tk.Frame(wrap, bg=PANEL, padx=18, pady=14)
        stats.pack(fill='x', pady=15)
        label(stats, f"{len(self.catalog['items'])} предметов    /    {len(self.catalog['sounds'])} групп    /    {len(self.references)} звуков", 13).pack(anchor='w')
        game_version = self.catalog.get('local_game_version')
        date = str(self.catalog.get('verified_at') if game_version else self.catalog.get('source_updated_at') or '')[:10]
        source_label = 'Эталоны из игры' if game_version else 'Справочник'
        version = game_version or self.catalog.get('data_version', '—')
        label(stats, f'{source_label}: {date}  ·  версия {version}', 10, MUTED).pack(anchor='w', pady=(5, 0))
        label(wrap, 'ЗВУК ИГРЫ ИЗ УСТРОЙСТВА ВОСПРОИЗВЕДЕНИЯ', 9, MUTED).pack(anchor='w')
        device_row = tk.Frame(wrap, bg=BG)
        device_row.pack(fill='x', pady=(7, 12))
        self.device_box = ttk.Combobox(device_row, state='readonly')
        self.device_box.pack(side='left', fill='x', expand=True)
        self.device_box.configure(values=['По умолчанию в Windows'])
        self.device_box.current(0)
        self.refresh_btn = button(device_row, 'Обновить', self.refresh_devices)
        self.refresh_btn.pack(side='left', padx=(8, 0))
        preferences = tk.Frame(wrap, bg=BG)
        preferences.pack(fill='x', pady=(0, 12))
        self.voice_check = tk.Checkbutton(preferences, text='Озвучивать до трёх кандидатов и цены контакта', variable=self.voice,
                                          bg=BG, fg=FG, selectcolor=CARD, activebackground=BG, activeforeground=FG)
        self.voice_check.pack(side='left')
        self.toggle_key_box = ttk.Combobox(preferences, state='readonly', width=9,
                                           values=TOGGLE_KEYS, textvariable=self.toggle_choice)
        self.toggle_key_box.pack(side='right')
        self.toggle_key_box.bind('<<ComboboxSelected>>', self.change_toggle_key)
        label(preferences, 'Клавиша открытия', 9, MUTED).pack(side='right', padx=(8, 8))
        actions = tk.Frame(wrap, bg=BG)
        actions.pack(fill='x')
        self.toggle_btn = button(actions, f'{self.toggle_key}   Включить поверх игры', self.toggle, primary=True)
        self.toggle_btn.pack(side='left')
        self.file_btn = button(actions, 'Проверить запись', self.open_audio)
        self.file_btn.pack(side='left', padx=8)
        button(actions, 'Пример', self.demo).pack(side='left')
        self.status_label = label(wrap, size=10, color=ACCENT, textvariable=self.state_text, anchor='w', justify='left', wraplength=740)
        self.status_label.pack(fill='x', pady=(14, 8))
        label(wrap, size=9, color=MUTED, textvariable=self.keyboard_text,
              anchor='w', justify='left', wraplength=740).pack(fill='x', pady=(0, 8))
        self.level = tk.Canvas(wrap, height=5, bg=CARD, highlightthickness=0)
        self.level.pack(fill='x', pady=(0, 16))
        help_box = tk.Frame(wrap, bg=PANEL, padx=16, pady=12)
        help_box.pack(fill='x')
        self.toggle_help = label(help_box, f'{self.toggle_key} — показать / скрыть и остановить прослушивание', 10, FG, anchor='w')
        self.toggle_help.pack(anchor='w', pady=2)
        for text in ['1–9 — форма предмета   ·   0 — все размеры   ·   F7 — следующая страница',
                     'F6 — включить мышь для перемещения окна и выбора кнопок']:
            label(help_box, text, 10, FG, anchor='w').pack(anchor='w', pady=2)
        label(wrap, 'Игра: оконный режим без рамки. Один звук может принадлежать разным предметам.\nЦены справочные, не текущий рынок; состояние предмета может менять цену.',
              9, MUTED, justify='left', wraplength=740).pack(anchor='w', pady=(13, 0))

    def setup_overlay(self):
        self.overlay = tk.Toplevel(self.root)
        self.overlay.withdraw()
        self.overlay.title('ABI Sound Scout — оверлей')
        self.overlay.protocol('WM_DELETE_WINDOW', self.stop)
        self.overlay.configure(bg=BG)
        self.overlay.overrideredirect(True)
        self.overlay.attributes('-topmost', True)
        self.overlay.attributes('-alpha', .96)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.overlay.geometry(f'900x550+{max(10, (sw-900)//2)}+{max(10, sh-625)}')
        head = tk.Frame(self.overlay, bg=PANEL, padx=18, pady=12)
        head.pack(fill='x')
        self.overlay_title = label(head, '●  СЛУШАЮ', 11, ACCENT)
        self.overlay_title.pack(side='left')
        self.overlay_hint = label(head, f'{self.toggle_key} скрыть  ·  F6 включить мышь', 10, MUTED)
        self.overlay_hint.pack(side='right')
        for widget in (head, self.overlay_title, self.overlay_hint):
            widget.bind('<ButtonPress-1>', self.drag_start)
            widget.bind('<B1-Motion>', self.drag_move)
        body = tk.Frame(self.overlay, bg=BG, padx=17, pady=13)
        body.pack(fill='both', expand=True)
        right = tk.Frame(body, bg=PANEL, padx=12, pady=12, width=216)
        right.pack(side='right', fill='y', padx=(18, 0))
        label(right, 'ФОРМА ПРЕДМЕТА', 10, MUTED).pack(anchor='w')
        self.filter_label = label(right, 'Все размеры', 13)
        self.filter_label.pack(anchor='w', pady=(4, 10))
        grid = tk.Frame(right, bg=PANEL)
        grid.pack()
        self.shape_canvases = []
        for n, (width, height) in enumerate(SHAPES, 1):
            canvas = tk.Canvas(grid, width=60, height=72, bg=CARD, highlightthickness=1,
                               highlightbackground=CARD, cursor='hand2')
            canvas.grid(row=(n-1)//3, column=(n-1)%3, padx=3, pady=3)
            canvas.create_text(7, 10, text=str(n), fill=MUTED, anchor='w', font=('Segoe UI', 9))
            unit = min(10, 36 / max(width, height))
            x0, y0 = (60-width*unit)/2, 19
            for y in range(height):
                for x in range(width):
                    canvas.create_rectangle(x0+x*unit, y0+y*unit, x0+(x+1)*unit-2, y0+(y+1)*unit-2,
                                            fill='#536c82', outline='')
            canvas.create_text(30, 62, text=f'{width}×{height}', fill=FG, font=('Segoe UI', 10))
            canvas.bind('<Button-1>', lambda event, key=n: self.select_shape(key))
            self.shape_canvases.append(canvas)
        button(right, '0   Все размеры', lambda: self.select_shape(0), pady=4).pack(fill='x', pady=(9, 4))
        label(right, 'Поворот учитывается:\n2×3 = 3×2', 9, MUTED, justify='left').pack(anchor='w', pady=4)
        left = tk.Frame(body, bg=BG)
        left.pack(side='left', fill='both', expand=True)
        self.match_heading = label(left, 'Перетащите скрытый предмет', 17, FG, anchor='w')
        self.match_heading.pack(fill='x')
        self.match_detail = label(left, 'Программа сравнит звук с эталонами.', 9, MUTED, anchor='w')
        self.match_detail.pack(fill='x', pady=(4, 11))
        column_names = tk.Frame(left, bg=BG)
        column_names.pack(fill='x')
        label(column_names, 'ВОЗМОЖНЫЙ ПРЕДМЕТ', 8, MUTED).pack(side='left')
        label(column_names, 'КОНТАКТ / СПРАВ.  ·  KOEN', 8, MUTED).pack(side='right')
        self.rows = tk.Frame(left, bg=BG)
        self.rows.pack(fill='both', expand=True, pady=(5, 0))
        self.page_label = label(left, 'Сходство звука — не вероятность выпадения предмета.', 9, MUTED, anchor='w')
        self.page_label.pack(fill='x', pady=(6, 0))
        self.page_label.bind('<Button-1>', lambda event: self.next_page())
        footer = tk.Frame(self.overlay, bg=PANEL, padx=18, pady=7)
        footer.pack(fill='x', side='bottom')
        checked = bool(self.catalog.get('local_game_version'))
        date = str(self.catalog.get('verified_at') if checked else self.catalog.get('source_updated_at') or '')[:10]
        label(footer, f"Полевой торговец · {'проверено по игре' if checked else 'база'} {date}", 9, MUTED).pack(side='left')
        self.mouse_state = label(footer, 'Мышь проходит в игру', 9, ACCENT)
        self.mouse_state.pack(side='right')

    def change_toggle_key(self, event=None):
        previous = self.toggle_key
        try:
            selected = normalize_toggle_key(self.toggle_choice.get())
            if selected == previous:
                return
            self.hotkeys.set_toggle_key(selected)
        except (ValueError, RuntimeError, OSError) as exc:
            self.toggle_choice.set(previous)
            self.state_text.set(f'Не удалось изменить клавишу открытия: {exc}')
            logging.exception('Opening key change failed')
            return
        self.toggle_key = selected
        self.toggle_choice.set(selected)
        self.refresh_toggle_labels()
        try:
            save_toggle_key(selected)
        except (OSError, ValueError):
            logging.exception('Opening key preference could not be saved')
            self.state_text.set(f'Клавиша {selected} работает до закрытия программы: сохранить настройку не удалось.')
        else:
            self.state_text.set(f'Клавиша открытия: {selected}. Настройка сохранена.')

    def refresh_toggle_labels(self):
        action = 'Остановить' if self.active else 'Включить поверх игры'
        self.toggle_btn.config(text=f'{self.toggle_key}   {action}')
        self.toggle_help.config(text=f'{self.toggle_key} — показать / скрыть и остановить прослушивание')
        if self.active:
            mouse = 'пропускать мышь' if self.editing else 'включить мышь'
            self.overlay_hint.config(text=f'{self.toggle_key} скрыть  ·  F6 {mouse}')
        else:
            self.overlay_hint.config(text=f'{self.toggle_key} закрыть · мышь включена')

    def refresh_devices(self):
        if getattr(self, 'active', False):
            return
        self.refresh_btn.config(state='disabled')
        def worker():
            try:
                from engine import LoopbackCapture
                self.service.events.put(('devices', 0, LoopbackCapture.list_devices()))
            except Exception as exc:
                self.service.events.put(('devices_error', 0, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def toggle(self):
        if not self.ready:
            self.state_text.set('Дождитесь загрузки эталонов.')
            return
        if self.active:
            self.stop()
            return
        self.token += 1
        self.active = True
        self.analyzing = False
        logging.info('Overlay start requested')
        self.editing = False
        self.results = []
        self.page = 0
        self.render_results()
        index = self.device_box.current()
        device = self.devices[index-1]['index'] if index > 0 else None
        self.service.commands.put(('start', self.token, device))
        self.hotkeys.active(True)
        self.overlay_title.config(text='●  ПОДКЛЮЧАЮ ЗВУК', fg=GOLD)
        self.state_text.set('Подключение к звуку Windows…')
        self.show_overlay(False)
        self.toggle_btn.config(text=f'{self.toggle_key}   Остановить')
        self.device_box.config(state='disabled')
        self.refresh_btn.config(state='disabled')

    def stop(self):
        logging.info('Overlay stop requested')
        self.active = False
        self.analyzing = False
        self.token += 1
        self.service.commands.put(('stop', self.token, None))
        self.hotkeys.active(False)
        self.overlay.withdraw()
        self.toggle_btn.config(text=f'{self.toggle_key}   Включить поверх игры')
        self.device_box.config(state='readonly')
        self.refresh_btn.config(state='normal')
        self.state_text.set(f'Остановлено. {self.toggle_key} — включить помощник.')
        self.level.delete('all')

    def show_overlay(self, editable):
        self.editing = editable
        self.overlay.overrideredirect(not editable)
        self.overlay.attributes('-alpha', 1.0 if editable else .97)
        # Apply NOACTIVATE while hidden, then map the window without focus theft.
        overlay_style(self.overlay, editable)
        self.overlay.deiconify()
        overlay_style(self.overlay, editable)
        self.overlay_hint.config(text=f'{self.toggle_key} скрыть  ·  F6 пропускать мышь' if editable else f'{self.toggle_key} скрыть  ·  F6 включить мышь')
        self.mouse_state.config(text='Мышь включена · тяните за шапку' if editable else 'Мышь проходит в игру')

    def drag_start(self, event):
        if self.editing:
            self.drag_offset = (event.x_root-self.overlay.winfo_x(), event.y_root-self.overlay.winfo_y())

    def drag_move(self, event):
        if self.editing and hasattr(self, 'drag_offset'):
            x, y = event.x_root-self.drag_offset[0], event.y_root-self.drag_offset[1]
            self.overlay.geometry(f'+{max(0, x)}+{max(0, y)}')

    def select_shape(self, key):
        logging.info('Shape filter selected: %s', key)
        self.selected, self.page = key, 0
        for n, canvas in enumerate(self.shape_canvases, 1):
            canvas.config(highlightbackground=ACCENT if n == key else CARD,
                          bg='#29483f' if n == key else CARD)
        shape = SHAPES[key-1] if key else None
        self.filter_label.config(text=f'{shape[0]}×{shape[1]}' if shape else 'Все размеры')
        self.render_results()

    def next_page(self):
        self.page += 1
        self.render_results()

    def render_results(self):
        for child in self.rows.winfo_children():
            child.destroy()
        rows = candidates(self.catalog, self.results, SHAPES[self.selected-1] if self.selected else None)
        total_pages = max(1, math.ceil(len(rows)/5))
        self.page %= total_pages
        if not self.results:
            self.match_heading.config(text='Перетащите скрытый предмет')
            self.match_detail.config(text='Ожидаю узнаваемый звук перемещения.')
            label(self.rows, 'Здесь появятся возможные предметы\nи справочные цены.', 12, MUTED,
                  justify='left').pack(anchor='w', pady=45)
        else:
            score = max(result['score'] for result in self.results)
            groups = ', '.join(str(r['group_id']) for r in self.results)
            self.match_heading.config(text=f'Кандидатов: {len(rows)}')
            self.match_detail.config(text=f'Группа {groups}  ·  сходство звука {score:.0%}')
            if not rows:
                label(self.rows, 'В этой группе нет предметов выбранной формы.\nНажмите 0 для всех размеров.', 11, MUTED,
                      justify='left', wraplength=500).pack(anchor='w', pady=35)
            for item in rows[self.page*5:(self.page+1)*5]:
                row = tk.Frame(self.rows, bg=CARD, padx=10, pady=6)
                row.pack(fill='x', pady=2)
                price = tk.Frame(row, bg=CARD)
                price.pack(side='right', padx=(8, 0))
                label(price, money(item.get('contact_price')), 11, FG).pack(anchor='e')
                label(price, '/ '+money(item.get('reference_price')), 8, MUTED).pack(anchor='e')
                label(row, item.get('name_ru') or item['name'], 10, RARITY.get(item.get('rarity'), FG),
                      anchor='w', wraplength=360, justify='left').pack(fill='x')
                label(row, f"{item['width']}×{item['height']}   ·   группа {item['sound_group']}", 8, MUTED).pack(anchor='w')
        self.page_label.config(text=f'Страница {self.page+1}/{total_pages}  ·  F7 далее  ·  сходство ≠ шанс выпадения')

    def open_audio(self):
        if not self.ready:
            return
        path = filedialog.askopenfilename(title='Короткая запись звука (до 30 секунд)',
                                          filetypes=[('Audio', '*.wav *.ogg *.flac'), ('Все файлы', '*.*')])
        if path:
            self.analyze_file(path)

    def demo(self):
        if self.ready:
            self.analyze_file(self.references[0]['path'])

    def analyze_file(self, path):
        self.stop()
        self.analyzing = True
        self.service.commands.put(('file', self.token, str(path)))
        self.state_text.set('Сравнение записи с эталонами…')
        self.overlay_title.config(text='ПРОВЕРКА ФАЙЛА · БЕЗ ПРОСЛУШИВАНИЯ', fg=GOLD)
        self.show_overlay(True)
        self.overlay_hint.config(text=f'{self.toggle_key} закрыть · мышь включена')

    def speak(self, results):
        rows = candidates(self.catalog, results, SHAPES[self.selected-1] if self.selected else None)
        if not self.voice.get() or not rows or self.speaking or time.monotonic()-self.last_spoken < 8:
            return
        signature = tuple(i['id'] for i in rows)
        if signature == self.spoken_signature:
            return
        self.spoken_signature, self.last_spoken = signature, time.monotonic()
        self.speaking = True
        text = 'Возможные предметы. ' + '. '.join(
            f"{r.get('name_ru') or r['name']}. Цена контакта {r.get('contact_price') or 'неизвестна'}" for r in rows[:3])
        def worker():
            try:
                # Only base64 ASCII enters PowerShell source. Encoding the entire
                # script alone would not protect apostrophes in catalog names.
                payload = base64.b64encode(text.encode('utf-8')).decode('ascii')
                script = "Add-Type -AssemblyName System.Speech; $scoutVoice = New-Object System.Speech.Synthesis.SpeechSynthesizer; $scoutText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+payload+"')); $scoutVoice.Speak($scoutText)"
                encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
                subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=45,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            except Exception:
                logging.exception('Speech unavailable')
                self.service.events.put(('speech_error', self.token, None))
            finally:
                self.speaking = False
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        if self.closing:
            return
        try:
            while True:
                kind, value = self.hotkeys.events.get_nowait()
                if kind == 'hotkey_error':
                    self.keyboard_text.set('Клавиатура: '+str(value))
                    logging.error('Keyboard: %s', value)
                    continue
                if kind == 'hotkey_ready':
                    self.keyboard_text.set('Клавиатура: работает в фоне · последнее нажатие: —')
                    logging.info('Keyboard ready: %s', value)
                    continue
                if kind != 'key':
                    continue
                key_name = {'toggle': self.toggle_key, 'edit': 'F6', 'next': 'F7'}.get(value, str(value))
                self.keyboard_text.set(f'Клавиатура: принято {key_name} в {time.strftime("%H:%M:%S")}')
                logging.info('Shortcut %s; listening_before=%s', key_name, self.active)
                if value == 'toggle':
                    if not self.active and self.overlay.state() != 'withdrawn':
                        self.overlay.withdraw()
                    else:
                        self.toggle()
                elif value == 'edit' and self.active:
                    self.show_overlay(not self.editing)
                elif value == 'next' and self.active:
                    self.next_page()
                elif isinstance(value, int) and self.active:
                    self.select_shape(value)
        except queue.Empty:
            pass
        try:
            while True:
                kind, token, value = self.service.events.get_nowait()
                if kind == 'ready':
                    self.ready = True
                    self.state_text.set(f'Готово. Откройте торговца и нажмите {self.toggle_key}.')
                    continue
                if kind == 'devices':
                    self.devices = value
                    self.device_box.configure(values=['По умолчанию в Windows'] + [d['name'] for d in value])
                    self.device_box.current(0)
                    if not self.active:
                        self.refresh_btn.config(state='normal')
                    continue
                if kind == 'devices_error':
                    self.state_text.set('Ошибка устройств: '+value)
                    self.refresh_btn.config(state='normal')
                    continue
                if kind == 'fatal':
                    if self.active or self.analyzing:
                        self.stop()
                    self.ready = False
                    self.state_text.set('Ошибка базы: '+value)
                    continue
                if token != self.token:
                    continue
                if kind == 'listening':
                    self.overlay_title.config(text='●  СЛУШАЮ', fg=ACCENT)
                    self.state_text.set('Слушаю: '+value)
                elif kind == 'error':
                    self.stop()
                    self.state_text.set('Ошибка: '+value)
                elif kind == 'speech_error':
                    self.state_text.set('Озвучивание недоступно. Результаты остаются на экране.')
                elif kind in ('result', 'file_result'):
                    results, level = value if kind == 'result' else (value, 0)
                    self.level.delete('all')
                    width = max(0, min(1, (20*math.log10(max(level, 1e-8))+65)/65))
                    self.level.create_rectangle(0, 0, self.level.winfo_width()*width, 5, fill=ACCENT, outline='')
                    if results:
                        self.last_found = time.monotonic()
                        changed = [(r['group_id'], round(r['score'], 2)) for r in results] != [(r['group_id'], round(r['score'], 2)) for r in self.results]
                        groups_changed = {r['group_id'] for r in results} != {r['group_id'] for r in self.results}
                        self.results = results
                        if changed:
                            if groups_changed:
                                self.page = 0
                            self.render_results()
                            if kind == 'result':
                                self.speak(results)
                    elif kind == 'file_result':
                        self.results = []
                        self.render_results()
                        self.match_heading.config(text='Уверенного совпадения нет')
                        self.match_detail.config(text='Попробуйте более чистую запись перемещения предмета.')
                    if kind == 'file_result':
                        self.analyzing = False
                        self.state_text.set('Проверка файла завершена. Живое прослушивание выключено.')
                    elif self.results and time.monotonic()-self.last_found > 3:
                        self.overlay_title.config(text='●  СЛУШАЮ · ПОКАЗАН ПРЕДЫДУЩИЙ ЗВУК', fg=GOLD)
                    elif self.active:
                        self.overlay_title.config(text='●  СЛУШАЮ', fg=ACCENT)
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    def close(self):
        self.closing = True
        self.service.commands.put(('quit', self.token+1, None))
        self.hotkeys.close()
        self.service.thread.join(2)
        self.root.destroy()


def main():
    if '--self-test' in sys.argv:
        if len(sys.argv) != 3 or sys.argv[1] != '--self-test':
            raise SystemExit('Usage: ABI Sound Scout --self-test RESULT_PATH')
        from selftest import run
        raise SystemExit(run(Path(sys.argv[2])))
    log_dir = Path(os.environ.get('LOCALAPPDATA', str(BASE))) / 'ABI Sound Scout'
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(handlers=[RotatingFileHandler(log_dir/'app.log', maxBytes=256_000,
                                                     backupCount=2, encoding='utf-8')], level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    logging.info('Starting ABI Sound Scout %s', VERSION)
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    # Match pixel-based grid geometry at every Windows display scale.
    root.tk.call('tk', 'scaling', 4/3)
    instance = None
    try:
        instance = SingleInstance()
        if instance.already_running:
            messagebox.showinfo('ABI Sound Scout', 'Программа уже открыта. Используйте её окно или выбранную клавишу открытия.', parent=root)
            root.destroy()
            instance.close()
            return
        app = Scout(root)
    except Exception as exc:
        logging.exception('Startup')
        messagebox.showerror('ABI Sound Scout', f'Не удалось запустить программу:\n{exc}', parent=root)
        root.destroy()
        if instance:
            instance.close()
        return
    try:
        root.mainloop()
    finally:
        instance.close()


if __name__ == '__main__':
    main()
