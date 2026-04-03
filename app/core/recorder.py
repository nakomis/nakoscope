"""Recorder: coordinates a CaptureDevice with a StorageBackend."""

import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

from .device import CaptureDevice
from .storage import StorageBackend


@dataclass
class RecorderStats:
    session_id: str
    n_frames: int = 0
    n_samples: int = 0
    elapsed_s: float = 0.0
    running: bool = False


class Recorder:
    """Pulls frames from a CaptureDevice and writes them to HDF5Storage.

    Usage (from CLI or GUI)::

        recorder = Recorder(device, storage)
        recorder.start(notes='button press test')
        # ... user waits ...
        stats = recorder.stop()
    """

    def __init__(self, device: CaptureDevice, storage: StorageBackend,
                 on_frame: Optional[Callable[[RecorderStats], None]] = None):
        """
        Args:
            device:     Connected, configured CaptureDevice.
            storage:    HDF5Storage instance.
            on_frame:   Optional callback invoked after each frame with current stats.
                        Called from the capture thread — keep it fast.
        """
        self._device    = device
        self._storage   = storage
        self._on_frame  = on_frame
        self._thread:   Optional[threading.Thread] = None
        self._stop_evt  = threading.Event()
        self._stats:    Optional[RecorderStats] = None
        self._lock      = threading.Lock()

    @property
    def stats(self) -> Optional[RecorderStats]:
        with self._lock:
            return self._stats

    def start(self, notes: str = '') -> str:
        """Begin recording. Returns the session_id."""
        if self._thread and self._thread.is_alive():
            raise RuntimeError('Recorder is already running.')

        info       = self._device.info()
        session_id = self._storage.start_session(
            notes         = notes,
            device_name   = info.name,
            device_serial = info.serial,
        )

        self._stop_evt.clear()
        with self._lock:
            self._stats = RecorderStats(session_id=session_id, running=True)

        self._thread = threading.Thread(
            target=self._run,
            args=(session_id,),
            daemon=True,
            name='recorder',
        )
        self._t_start = time.monotonic()
        self._thread.start()
        return session_id

    def stop(self) -> RecorderStats:
        """Signal the capture thread to stop and wait for it to finish."""
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=10)
        with self._lock:
            if self._stats:
                self._stats.running = False
            return self._stats

    def _run(self, session_id: str) -> None:
        try:
            for frame in self._device.frames():
                if self._stop_evt.is_set():
                    break

                for ch_name, ch_data in frame.channels.items():
                    self._storage.append_frame(
                        session_id  = session_id,
                        channel_name = ch_name,
                        samples      = ch_data.samples,
                        sample_rate  = ch_data.sample_rate,
                        v_range      = ch_data.v_range,
                    )

                with self._lock:
                    self._stats.n_frames  += 1
                    self._stats.n_samples += sum(
                        len(ch.samples) for ch in frame.channels.values()
                    )
                    self._stats.elapsed_s = time.monotonic() - self._t_start

                if self._on_frame:
                    self._on_frame(self._stats)

        finally:
            with self._lock:
                n_frames = self._stats.n_frames if self._stats else 0
            self._storage.end_session(session_id, n_frames=n_frames)
