"""Local reference matching and Windows output-loopback capture.

Scores are audio similarities, not probabilities of an item being present.
No process handles, microphone input, network access, or persistent recording.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import RLock
import time

import numpy as np
import soundfile as sf


MATCH_RATE = 16000
DEFAULT_THRESHOLD = 0.70


def _mono(audio: np.ndarray) -> np.ndarray:
    values = np.asarray(audio, dtype=np.float32)
    if values.ndim == 2:
        values = values.mean(axis=1)
    if values.ndim != 1:
        raise ValueError("Audio must contain mono samples or frames × channels")
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)


@lru_cache(maxsize=8)
def _resample_filter(sample_rate: int) -> np.ndarray:
    """Windowed sinc antialiasing FIR with a Blackman window."""
    # About 2 ms on either side. Blackman window avoids aliasing high-frequency
    # game audio when the output endpoint is downsampled for matching.
    half = max(32, int(sample_rate * .002))
    positions = np.arange(-half, half + 1, dtype=np.float64)
    cutoff = min(MATCH_RATE, sample_rate) * .43 / sample_rate
    taps = 2 * cutoff * np.sinc(2 * cutoff * positions) * np.blackman(len(positions))
    return taps / taps.sum()


def _prepare(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    if not isinstance(sample_rate, (int, np.integer)) or sample_rate <= 0:
        raise ValueError("Sample rate must be a positive integer")
    values = _mono(audio)
    if not len(values):
        return values
    if sample_rate != MATCH_RATE:
        taps = _resample_filter(int(sample_rate))
        # np.convolve(mode='same') returns the longer operand; explicitly slice
        # the full convolution to preserve even very short input lengths.
        filtered = np.convolve(values, taps, mode="full")
        delay = (len(taps) - 1) // 2
        filtered = filtered[delay:delay + len(values)]
        positions = np.arange(max(1, round(len(values) * MATCH_RATE / sample_rate))) * (sample_rate / MATCH_RATE)
        values = np.interp(positions, np.arange(len(values)), filtered)
    # Remove slowly varying background through a centered moving-average FIR.
    # Cumulative sums keep this O(n), and applying the identical filter to both
    # sides preserves the event's waveform and sample alignment.
    width = 161
    padded = np.pad(values, (width // 2, width // 2))
    cumulative = np.concatenate(([0.0], np.cumsum(padded, dtype=np.float64)))
    background = (cumulative[width:] - cumulative[:-width]) / width
    return np.asarray(values - background, dtype=np.float32)


def _trim(values: np.ndarray) -> np.ndarray:
    energy = np.cumsum(values.astype(np.float64) ** 2)
    if not len(energy) or energy[-1] < 1e-12:
        raise ValueError("Reference is silent")
    # Keep most of the event, rather than its encoding silence. Padding retains
    # the attack and release, which distinguish similar inventory sounds.
    pad = int(0.015 * MATCH_RATE)
    left = max(0, int(np.searchsorted(energy, energy[-1] * 0.001)) - pad)
    right = min(len(values), int(np.searchsorted(energy, energy[-1] * 0.999)) + pad + 1)
    return values[left:right]


def _spectrum(values: np.ndarray) -> np.ndarray:
    window = min(512, len(values))
    if window < 2:
        return np.zeros(237)
    frames = np.lib.stride_tricks.sliding_window_view(values, window)[::max(1, window // 2)]
    transforms = np.fft.rfft(frames * np.hanning(window), n=512, axis=1)
    power = np.mean(np.abs(transforms) ** 2, axis=0)
    # Square root power limits the influence of a single loud frequency.
    bands = np.sqrt(np.maximum(power[3:240], 0.0))
    length = np.linalg.norm(bands)
    return bands / max(length, 1e-15)


@dataclass
class _Reference:
    group_id: int
    kind: str
    samples: np.ndarray
    energy: float
    spectrum: np.ndarray
    fft_cache: dict[int, np.ndarray] = field(default_factory=dict)


class Matcher:
    """Find complete reference events anywhere in a rolling audio window.

    ``references`` entries contain group_id, kind and path. Errors in individual
    clips are exposed through ``errors`` so one missing file does not disable
    the whole catalog. ``match`` returns one best result per accepted group.
    """

    def __init__(self, references: list[dict], threshold: float = DEFAULT_THRESHOLD):
        self.threshold = float(threshold)
        self.errors: list[str] = []
        self.references: list[_Reference] = []
        for entry in references:
            try:
                samples, rate = sf.read(Path(entry["path"]), dtype="float32", always_2d=False)
                samples = _trim(_prepare(samples, int(rate)))
                if len(samples) < int(MATCH_RATE * 0.04):
                    raise ValueError("Reference contains less than 40 ms of audio")
                # Mean-centered reference makes correlation invariant to any
                # residual constant offset inside a candidate segment.
                samples = samples - samples.mean()
                self.references.append(_Reference(
                    group_id=int(entry["group_id"]), kind=str(entry["kind"]),
                    samples=samples, energy=float(np.dot(samples, samples)),
                    spectrum=_spectrum(samples),
                ))
            except Exception as exc:
                self.errors.append(f"{entry.get('path', '?')}: {exc}")

    def match(self, audio: np.ndarray, sample_rate: int) -> list[dict]:
        values = _prepare(audio, sample_rate)
        if not self.references or len(values) < MATCH_RATE // 25:
            return []
        if float(np.max(np.abs(values), initial=0)) < 1e-5:
            return []
        # Accumulate in float64: float32 cancellation in quiet parts of a long
        # window otherwise causes normalized-correlation spikes.
        squares = np.concatenate(([0.0], np.cumsum(values.astype(np.float64) ** 2)))
        sums = np.concatenate(([0.0], np.cumsum(values, dtype=np.float64)))
        energy_by_length: dict[int, np.ndarray] = {}
        best_by_group: dict[int, dict] = {}
        fft_size = 1 << (len(values) + max(len(r.samples) for r in self.references) - 2).bit_length()
        transformed = np.fft.rfft(values, n=fft_size)
        for reference in self.references:
            count = len(reference.samples)
            if count > len(values):
                continue
            if count not in energy_by_length:
                energy = squares[count:] - squares[:-count]
                segment_sum = sums[count:] - sums[:-count]
                energy_by_length[count] = np.maximum(energy - segment_sum ** 2 / count, 0.0)
            energy = energy_by_length[count]
            if fft_size not in reference.fft_cache:
                if len(reference.fft_cache) >= 4:
                    reference.fft_cache.clear()
                reference.fft_cache[fft_size] = np.conj(np.fft.rfft(reference.samples, n=fft_size))
            correlation = np.fft.irfft(transformed * reference.fft_cache[fft_size], n=fft_size)[:len(values) - count + 1]
            denominator = np.sqrt(energy * reference.energy)
            # FFT roundoff divided by near-zero energy in a silent segment can
            # look like a perfect correlation. Ignore segments more than 70 dB
            # below the window's strongest segment, and numerical negatives.
            valid_energy = energy > max(1e-12, float(np.max(energy)) * 1e-7)
            normalized = np.divide(np.abs(correlation), denominator,
                                   out=np.zeros_like(correlation), where=valid_energy & (denominator > 1e-9))
            offset = int(np.argmax(normalized))
            waveform = float(np.clip(normalized[offset], 0.0, 1.0))
            # Spectral similarity may improve a noisy match, but must never
            # convert a merely similar timbre into a recognized event.
            if waveform < 0.55:
                continue
            spectral = float(np.clip(np.dot(_spectrum(values[offset:offset + count]),
                                            reference.spectrum), 0.0, 1.0))
            score = float(np.clip(0.82 * waveform + 0.18 * spectral, 0.0, 1.0))
            if score < self.threshold:
                continue
            result = {"group_id": reference.group_id, "kind": reference.kind,
                      "score": score, "waveform": waveform, "spectral": spectral,
                      "offset_seconds": offset / MATCH_RATE,
                      "end_seconds": (offset + count) / MATCH_RATE}
            prior = best_by_group.get(reference.group_id)
            if prior is None or prior["score"] < score:
                best_by_group[reference.group_id] = result
        return sorted(best_by_group.values(), key=lambda row: row["score"], reverse=True)


class LoopbackCapture:
    """Capture the selected Windows playback endpoint via WASAPI loopback.

    ``None`` selects the current default output endpoint. This class never
    substitutes a recording/microphone endpoint when loopback is unavailable.
    Audio is kept in a bounded RAM buffer. Call ``stop`` when the overlay hides.
    """

    def __init__(self, device_index: int | None = None):
        self.device_index = device_index
        self.sample_rate = 48000
        self.device_name = ""
        self.error: str | None = None
        self.errors: list[str] = []
        self._audio = None
        self._stream = None
        self._lock = RLock()
        self._chunks: deque[np.ndarray] = deque()
        self._buffer_samples = 0
        self._last_received = 0.0
        self._channels = 2

    @staticmethod
    def list_devices() -> list[dict]:
        import pyaudiowpatch as pa
        with pa.PyAudio() as audio:
            try:
                default = audio.get_default_wasapi_loopback()
                default_index = int(default["index"])
            except (OSError, ValueError):
                default_index = None
            return [{"index": int(device["index"]), "name": str(device["name"]),
                     "default": int(device["index"]) == default_index,
                     "sample_rate": int(device["defaultSampleRate"])}
                    for device in audio.get_loopback_device_info_generator()
                    if device.get("isLoopbackDevice") and device.get("maxInputChannels", 0) > 0]

    @property
    def running(self) -> bool:
        try:
            return bool(self._stream and self._stream.is_active())
        except OSError:
            return False

    def start(self) -> None:
        import pyaudiowpatch as pa
        self.stop()
        self.error = None
        self.errors.clear()
        try:
            self._audio = pa.PyAudio()
            device = (self._audio.get_default_wasapi_loopback() if self.device_index is None
                      else self._audio.get_device_info_by_index(int(self.device_index)))
            if not device.get("isLoopbackDevice") or int(device.get("maxInputChannels", 0)) < 1:
                raise RuntimeError("Selected device is not a WASAPI output-loopback endpoint")
            self.device_name = str(device["name"])
            self.sample_rate = int(device["defaultSampleRate"])
            self._channels = int(device["maxInputChannels"])
            with self._lock:
                self._chunks.clear()
                self._buffer_samples = 0
                self._last_received = 0.0

            def callback(in_data, frame_count, time_info, status):
                try:
                    if status:
                        # Input-overflow is recoverable; clear a discontinuous
                        # buffer instead of attempting to recognize across it.
                        with self._lock:
                            self._chunks.clear()
                            self._buffer_samples = 0
                    frames = np.frombuffer(in_data, dtype=np.float32).reshape(-1, self._channels)
                    mono = frames.mean(axis=1, dtype=np.float32)
                    with self._lock:
                        self._chunks.append(mono.copy())
                        self._buffer_samples += len(mono)
                        maximum = self.sample_rate * 6
                        while self._chunks and self._buffer_samples > maximum + len(self._chunks[0]):
                            self._buffer_samples -= len(self._chunks.popleft())
                        self._last_received = time.monotonic()
                    return (None, pa.paContinue)
                except Exception as exc:
                    self.error = str(exc)
                    self.errors.append(self.error)
                    return (None, pa.paAbort)

            self._stream = self._audio.open(
                format=pa.paFloat32, channels=self._channels, rate=self.sample_rate,
                input=True, input_device_index=int(device["index"]),
                frames_per_buffer=1024, stream_callback=callback,
            )
        except Exception as exc:
            self.error = str(exc)
            self.errors.append(self.error)
            self.stop()
            raise RuntimeError(f"WASAPI loopback: {exc}") from exc

    def snapshot(self, seconds: float = 3.0) -> tuple[np.ndarray, int, float]:
        count = max(1, min(int(float(seconds) * self.sample_rate), self.sample_rate * 6))
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32), self.sample_rate, 0.0
            values = np.concatenate(tuple(self._chunks))[-count:]
            age = time.monotonic() - self._last_received
        # Some endpoints stop sending packets when playback is silent. Do not
        # repeatedly report the final old event as if it had just occurred.
        if age > 0.4:
            return np.zeros(0, dtype=np.float32), self.sample_rate, 0.0
        recent = values[-min(len(values), self.sample_rate // 10):]
        level = float(np.sqrt(np.mean(recent.astype(np.float64) ** 2))) if len(recent) else 0.0
        return values, self.sample_rate, level

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        audio, self._audio = self._audio, None
        if stream:
            try:
                if stream.is_active():
                    stream.stop_stream()
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass
        if audio:
            audio.terminate()
        with self._lock:
            self._chunks.clear()
            self._buffer_samples = 0
            self._last_received = 0.0

