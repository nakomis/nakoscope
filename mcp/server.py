#!/usr/bin/env python3
"""Oscilloscope MCP Server.

Provides Claude with read access to the nakoscope datastore and remote
control of the capture device, so that waveform recordings can be driven
end-to-end from a conversation.

Tools:
  list_sessions      — browse recent recording sessions
  get_session_info   — full metadata for one session
  get_waveform       — voltage samples for a channel (downsampled if large)
  record_for         — run a bounded recording for N seconds and return stats
  start_recording    — open the device and begin a recording (returns session_id)
  stop_recording     — stop the in-flight recording started by start_recording
  capture_triggered  — one-shot edge-triggered frame at up to 100 MS/s (blocking)
  arm_triggered      — the same, armed in the background (fire, then wait)
  wait_triggered     — wait for the armed capture's result
"""

import sys
import threading
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Locate the storage module — the app directory lives alongside this one.
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root / 'app'))

from core.storage import StorageBackend, create_backend

mcp = FastMCP('nakoscope')

_backend: StorageBackend | None = None


def _storage() -> StorageBackend:
    global _backend
    if _backend is None:
        _backend = create_backend()
    return _backend


# ── Capture session state (start_recording / stop_recording) ─────────────────
# Only one recording may be in flight at a time. record_for uses the same
# lock so a bounded run can't collide with a manual start_recording.

_capture_lock = threading.Lock()
_active: dict | None = None  # {'recorder', 'device', 'session_id'}


def _resolve_capture_args(
    channels: Optional[list[str]],
    sample_rate: Optional[float],
    v_range: Optional[float],
    coupling: Optional[str],
    probe: Optional[float],
) -> dict:
    """Fill in capture parameters from ~/.nakoscope.yaml defaults."""
    from core.config import get
    cap = get('capture') or {}
    return dict(
        channels          = channels    if channels    is not None else cap.get('channels', ['CH1', 'CH2']),
        sample_rate       = sample_rate if sample_rate is not None else float(cap.get('sample_rate', 250_000)),
        v_range           = v_range     if v_range     is not None else float(cap.get('v_range', 10.0)),
        coupling          = coupling    if coupling    is not None else cap.get('coupling', 'DC'),
        probe_attenuation = probe       if probe       is not None else float(cap.get('probe', 1.0)),
    )


def _open_recorder(notes: str, capture: dict) -> tuple:
    """Connect the scope, start a recorder, return (recorder, device, session_id)."""
    from core.recorder import Recorder
    from core.devices.vds1022 import VDS1022Device

    device  = VDS1022Device()
    storage = _storage()

    device.connect()
    try:
        device.configure(**capture)
        recorder = Recorder(device, storage)
        session_id = recorder.start(notes=notes)
    except Exception:
        device.disconnect()
        raise

    return recorder, device, session_id


def _close_recorder(recorder, device) -> dict:
    """Stop the recorder, disconnect the device, return a stats dict."""
    try:
        stats = recorder.stop()
    finally:
        device.disconnect()
    return {
        'session_id': stats.session_id,
        'n_frames':   stats.n_frames,
        'n_samples':  stats.n_samples,
        'elapsed_s':  stats.elapsed_s,
        'running':    stats.running,
    }


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_sessions(
    limit: int = 20,
    since: str = '',
    search: str = '',
) -> list[dict]:
    """List nakoscope recording sessions, newest first.

    Args:
        limit:  Maximum number of sessions to return (default 20).
        since:  Optional ISO-8601 datetime string — only return sessions started after this.
        search: Optional text to filter by session notes.

    Returns:
        List of session summaries with keys:
          session_id, started_at, ended_at, notes, device_name, device_serial,
          n_frames, n_samples, channels, duration_s
    """
    return _storage().list_sessions(
        limit  = limit,
        since  = since or None,
        search = search or None,
    )


@mcp.tool()
def get_session_info(session_id: str) -> dict:
    """Get full metadata and channel statistics for a recording session.

    Args:
        session_id: The session identifier (from list_sessions).

    Returns:
        Dict with keys:
          session_id, started_at, ended_at, notes, device_name, device_serial,
          n_frames, duration_s,
          channels: { ch1: { n_samples, sample_rate, v_range }, ... }
    """
    result = _storage().get_session(session_id)
    if result is None:
        raise ValueError(f'Session not found: {session_id}')
    return result


@mcp.tool()
def get_waveform(
    session_id: str,
    channel: str = 'ch1',
    max_points: int = 5000,
    start_s: float | None = None,
    end_s: float | None = None,
) -> dict:
    """Get waveform data for a channel from a recording session.

    Large captures are automatically downsampled to max_points for context
    efficiency. Set start_s/end_s to slice a time window (omit for the full range).

    Args:
        session_id: The session identifier.
        channel:    Channel name: 'ch1' or 'ch2' (case-insensitive).
        max_points: Maximum number of voltage samples to return (default 5000).
        start_s:    Start of the window in seconds (omit = beginning). Seconds from
                    session start, or from the trigger for triggered captures
                    (negative = pre-trigger).
        end_s:      End of the window in seconds (omit = end), same time base.

    Returns:
        Dict with keys:
          samples      — list of float voltages (Volts)
          time_axis    — list of float timestamps (seconds from session start;
                         for triggered captures, seconds from the trigger)
          sample_rate  — original sample rate in Hz
          v_range      — full-scale voltage range (10 divs, Volts)
          downsampled  — True if data was decimated to fit max_points
          original_n   — number of samples in the requested window before decimation
          time_offset  — time of the first stored sample (negative = pre-trigger)
    """
    result = _storage().get_waveform(
        session_id = session_id,
        channel    = channel,
        max_points = max_points,
        start_s    = start_s,
        end_s      = end_s,
    )
    if result is None:
        raise ValueError(f'Channel {channel!r} not found in session {session_id!r}')
    return result


# ── Capture control tools ─────────────────────────────────────────────────────

@mcp.tool()
def record_for(
    seconds: float,
    notes: str = '',
    channels: list[str] | None = None,
    sample_rate: float | None = None,
    v_range: float | None = None,
    coupling: str | None = None,
    probe: float | None = None,
) -> dict:
    """Run a bounded recording: start the scope, wait `seconds`, stop and return stats.

    This is the workhorse for scripted bench tests where the duration is known
    up-front (e.g. publishing an MQTT command sequence over a fixed window).
    The call blocks for roughly `seconds` plus a small device teardown delay.

    Any capture parameter left as None falls back to the defaults in
    ~/.nakoscope.yaml (capture.* section).

    Args:
        seconds:     How long to capture for. Bounded to [0.5, 600].
        notes:       Free-text notes attached to the session.
        channels:    Channels to enable, e.g. ['CH1'] or ['CH1', 'CH2'].
        sample_rate: Sample rate in Hz.
        v_range:     Full-scale voltage range across 10 divs (e.g. 10 = ±5V).
        coupling:    'DC' or 'AC'.
        probe:       Probe attenuation (1.0 for x1, 10.0 for x10).

    Returns:
        Dict: { session_id, n_frames, n_samples, elapsed_s, running }
    """
    if seconds < 0.5 or seconds > 600:
        raise ValueError('seconds must be between 0.5 and 600')

    capture = _resolve_capture_args(channels, sample_rate, v_range, coupling, probe)

    if not _capture_lock.acquire(blocking=False):
        raise RuntimeError('A recording is already in progress')
    try:
        global _active
        if _active is not None:
            raise RuntimeError('A recording is already in progress')

        recorder, device, _session_id = _open_recorder(notes, capture)
        import time
        try:
            time.sleep(seconds)
        except BaseException:
            _close_recorder(recorder, device)
            raise
        return _close_recorder(recorder, device)
    finally:
        _capture_lock.release()


@mcp.tool()
def start_recording(
    notes: str = '',
    channels: list[str] | None = None,
    sample_rate: float | None = None,
    v_range: float | None = None,
    coupling: str | None = None,
    probe: float | None = None,
) -> dict:
    """Open the scope and begin a recording. Returns immediately.

    Use this when the duration is not known up-front. Pair with stop_recording
    to end the session. For bounded captures prefer record_for instead.

    Returns:
        Dict: { session_id }
    """
    capture = _resolve_capture_args(channels, sample_rate, v_range, coupling, probe)

    with _capture_lock:
        global _active
        if _active is not None:
            raise RuntimeError('A recording is already in progress')

        recorder, device, session_id = _open_recorder(notes, capture)
        _active = {'recorder': recorder, 'device': device, 'session_id': session_id}
        return {'session_id': session_id}


@mcp.tool()
def stop_recording() -> dict:
    """Stop the recording started by start_recording and return final stats.

    Returns:
        Dict: { session_id, n_frames, n_samples, elapsed_s, running }
    """
    with _capture_lock:
        global _active
        if _active is None:
            raise RuntimeError('No recording is in progress')

        recorder = _active['recorder']
        device   = _active['device']
        _active = None

    return _close_recorder(recorder, device)


# ── Triggered capture tools ───────────────────────────────────────────────────
# A triggered capture holds _capture_lock from arming until the frame is stored
# (or the trigger times out), so it can't collide with a recording.

_armed: dict | None = None  # {'thread', 'done': Event, 'result', 'error'}


def _triggered_worker(settings, notes, slot):
    from core.devices.vds1022 import VDS1022Device
    from core.triggered import run_triggered
    try:
        slot['result'] = run_triggered(VDS1022Device(), _storage(), settings, notes=notes)
    except Exception as e:  # noqa: BLE001 — reported by wait_triggered
        slot['error'] = f'{type(e).__name__}: {e}'
    finally:
        _capture_lock.release()
        slot['done'].set()


def _triggered_result(slot) -> dict:
    if slot['error']:
        raise RuntimeError(slot['error'])
    sid = slot['result']
    if sid is None:
        return {'status': 'timeout', 'session_id': None}
    session = _storage().get_session(sid)
    levels = {}
    for ch in session['channels']:
        wf = _storage().get_waveform(sid, ch, max_points=10_000)
        v = wf['samples']
        levels[ch] = {'min': min(v), 'max': max(v), 'mean': sum(v) / len(v)}
    return {'status': 'captured', 'session_id': sid, 'levels': levels,
            'channels': session['channels']}


@mcp.tool()
def arm_triggered(
    trigger: str = 'CH1',
    level: float = 0.0,
    edge: str = 'rise',
    position: float = 0.5,
    sample_rate: float = 100e6,
    channels: list[str] | None = None,
    v_range: float = 5.0,
    offset: float = 0.5,
    coupling: str = 'DC',
    probe: float = 1.0,
    timeout: float = 30.0,
    notes: str = '',
) -> dict:
    """Arm a one-shot edge trigger in the background and return immediately.

    Fire the event (e.g. run the firmware command) after this returns, then
    call wait_triggered. One frame is ~5000 samples per channel, so the window
    is 5000 / sample_rate (50 µs at 100 MS/s). Stored time axes are
    trigger-relative: t = 0 is the trigger, negative times are pre-trigger.

    Args:
        trigger:     'CH1', 'CH2' or 'EXT' (Multi port as TTL trigger input).
        level:       Trigger level in volts at the probe tip (ignored for EXT).
        edge:        'rise' or 'fall'.
        position:    Trigger position in the frame, 0 (left) to 1 (right).
        sample_rate: Hz, snapped to the scope's rates (max 100e6).
        channels:    Channels to record (default ['CH1', 'CH2']).
        v_range:     Full-scale volts across 10 divs at the probe tip.
        offset:      Where 0 V sits on screen, 0 (bottom) to 1 (top).
        coupling:    'DC' or 'AC'.
        probe:       Probe attenuation (1 for x1, 10 for x10).
        timeout:     Seconds to wait for the trigger, at most 600.
        notes:       Free-text notes for the session.

    Returns:
        Dict: { armed: True, settings }
    """
    from core.triggered import TriggerSettings

    if timeout > 600:
        raise ValueError('timeout must be at most 600 seconds')
    settings = TriggerSettings(
        source=trigger, level=level, edge=edge, position=position,
        sample_rate=sample_rate, channels=channels or ['CH1', 'CH2'],
        v_range=v_range, offset=offset, coupling=coupling,
        probe_attenuation=probe, timeout=timeout,
    )

    global _armed
    if not _capture_lock.acquire(blocking=False):
        raise RuntimeError('The scope is busy (recording or already armed)')
    slot = {'done': threading.Event(), 'result': None, 'error': None}
    slot['thread'] = threading.Thread(
        target=_triggered_worker, args=(settings, notes, slot), daemon=True,
    )
    _armed = slot
    slot['thread'].start()
    return {'armed': True, 'settings': settings.to_metadata()}


@mcp.tool()
def wait_triggered(wait_s: float = 60.0) -> dict:
    """Wait for the capture started by arm_triggered.

    Args:
        wait_s: How long to block here before reporting 'waiting' (max 600).

    Returns:
        Dict: { status: 'captured' | 'timeout' | 'waiting', session_id,
                levels: { ch1: {min, max, mean}, ... }, channels }
        Read the samples with get_waveform(session_id, 'ch1', ...).
    """
    global _armed
    slot = _armed
    if slot is None:
        raise RuntimeError('Nothing is armed — call arm_triggered first')
    if not slot['done'].wait(min(max(wait_s, 0.0), 600.0)):
        return {'status': 'waiting', 'session_id': None}
    _armed = None
    return _triggered_result(slot)


@mcp.tool()
def capture_triggered(
    trigger: str = 'CH1',
    level: float = 0.0,
    edge: str = 'rise',
    position: float = 0.5,
    sample_rate: float = 100e6,
    channels: list[str] | None = None,
    v_range: float = 5.0,
    offset: float = 0.5,
    coupling: str = 'DC',
    probe: float = 1.0,
    timeout: float = 10.0,
    notes: str = '',
) -> dict:
    """Arm a one-shot edge trigger and block until it fires or times out.

    For repetitive signals that will trigger on their own. When something has
    to be fired after arming, use arm_triggered + wait_triggered instead.
    Arguments are as for arm_triggered.

    Returns:
        Dict as for wait_triggered ('captured' or 'timeout').
    """
    arm_triggered(trigger, level, edge, position, sample_rate, channels,
                  v_range, offset, coupling, probe, timeout, notes)
    return wait_triggered(timeout + 30)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == '__main__':
    main()
