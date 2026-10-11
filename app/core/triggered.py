"""Triggered one-shot capture: settings, timeout handling and storage.

Roll mode (recorder.py) streams continuously at up to ~1.25 MS/s. A triggered
capture instead arms the scope, waits for one edge, and fetches a single
~5000-sample frame per channel at up to 100 MS/s — the tool for looking at
MHz bus activity around one event (e.g. a DDR3 READ burst).

The device-specific part lives in VDS1022Device.capture_triggered(); this
module holds everything that can be tested without hardware.
"""

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TypeVar

from .device import CaptureFrame
from .storage import StorageBackend

SOURCES = ('CH1', 'CH2', 'EXT')
EDGES   = ('rise', 'fall')

T = TypeVar('T')


@dataclass
class TriggerSettings:
    """Everything needed to arm one triggered capture.

    source:      'CH1', 'CH2' or 'EXT' (the scope's Multi port as trigger in).
    level:       trigger level in volts (ignored for EXT, which is TTL).
    edge:        'rise' or 'fall'.
    position:    where the trigger sits in the frame, 0 (left) to 1 (right).
    sample_rate: Hz; snapped by the device to the nearest supported rate.
    channels:    channels to record.
    v_range:     full-scale volts across the 10 divisions, at the probe tip.
    offset:      where 0 V sits on the screen, 0 (bottom) to 1 (top).
    timeout:     seconds to wait for the trigger before giving up.
    """
    source:            str   = 'CH1'
    level:             float = 0.0
    edge:              str   = 'rise'
    position:          float = 0.5
    sample_rate:       float = 100e6
    channels:          list  = field(default_factory=lambda: ['CH1', 'CH2'])
    v_range:           float = 5.0
    offset:            float = 0.5
    coupling:          str   = 'DC'
    probe_attenuation: float = 1.0
    timeout:           float = 10.0

    def __post_init__(self):
        self.source   = self.source.upper()
        self.edge     = self.edge.lower()
        self.coupling = self.coupling.upper()
        self.channels = [c.upper() for c in self.channels]

        if self.source not in SOURCES:
            raise ValueError(f'source must be one of {SOURCES}, got {self.source!r}')
        if self.edge not in EDGES:
            raise ValueError(f'edge must be one of {EDGES}, got {self.edge!r}')
        if not self.channels or any(c not in ('CH1', 'CH2') for c in self.channels):
            raise ValueError(f"channels must be a non-empty subset of ['CH1', 'CH2'], got {self.channels}")
        if self.source in ('CH1', 'CH2') and self.source not in self.channels:
            raise ValueError(f'trigger source {self.source} must be one of the recorded channels')
        if not 0 <= self.position <= 1:
            raise ValueError(f'position must be between 0 and 1, got {self.position}')
        if not 0 <= self.offset <= 1:
            raise ValueError(f'offset must be between 0 and 1, got {self.offset}')
        if self.sample_rate <= 0 or self.v_range <= 0 or self.timeout <= 0:
            raise ValueError('sample_rate, v_range and timeout must be positive')

    def to_metadata(self) -> dict:
        return {'mode': 'triggered', **asdict(self)}


def fetch_with_timeout(fetch: Callable[[], T], stop: Callable[[], object],
                       timeout: float) -> T | None:
    """Call a blocking fetch(), calling stop() if it hasn't returned in time.

    fetch() is expected to return early (with None, or by raising
    StopIteration) once stop() has been called. Returns fetch()'s result, or
    None if it was stopped. A result that lands as the timer fires is kept.
    """
    timer = threading.Timer(timeout, stop)
    timer.daemon = True
    timer.start()
    try:
        return fetch()
    except StopIteration:
        return None
    finally:
        timer.cancel()


def save_capture(storage: StorageBackend, frame: CaptureFrame,
                 settings: TriggerSettings, notes: str = '',
                 device_name: str = '', device_serial: str = '') -> str:
    """Store one triggered frame as a session and return its session_id.

    Each channel's time_offset (seconds from the trigger to the first sample,
    negative for pre-trigger data) is stored so that read-back time axes are
    trigger-relative: t = 0 is the trigger.
    """
    session_id = storage.start_session(
        notes         = notes,
        device_name   = device_name,
        device_serial = device_serial,
        metadata      = settings.to_metadata(),
    )
    for ch in frame.channels.values():
        storage.append_frame(
            session_id, ch.name, ch.samples, ch.sample_rate, ch.v_range,
            time_offset = ch.time_offset,
        )
    storage.end_session(session_id, n_frames=1)
    return session_id


def run_triggered(device, storage: StorageBackend, settings: TriggerSettings,
                  notes: str = '') -> str | None:
    """Connect, arm, capture and store one triggered frame.

    device is a connected-or-not capture device with capture_triggered()
    (VDS1022Device). Returns the new session_id, or None if nothing triggered
    within the timeout — in which case no session is created.
    """
    device.connect()
    try:
        info  = device.info()
        frame = device.capture_triggered(settings)
    finally:
        device.disconnect()
    if frame is None:
        return None
    return save_capture(storage, frame, settings, notes=notes,
                        device_name=info.name, device_serial=info.serial)
