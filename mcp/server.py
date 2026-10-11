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
    start_s: float = 0.0,
    end_s: float = 0.0,
) -> dict:
    """Get waveform data for a channel from a recording session.

    Large captures are automatically downsampled to max_points for context
    efficiency. Set start_s/end_s to slice a time window (0.0 = full range).

    Args:
        session_id: The session identifier.
        channel:    Channel name: 'ch1' or 'ch2' (case-insensitive).
        max_points: Maximum number of voltage samples to return (default 5000).
        start_s:    Start of time window in seconds from session start (0 = beginning).
        end_s:      End of time window in seconds from session start (0 = end).

    Returns:
        Dict with keys:
          samples      — list of float voltages (Volts)
          time_axis    — list of float timestamps (seconds from session start)
          sample_rate  — original sample rate in Hz
          v_range      — full-scale voltage range (10 divs, Volts)
          downsampled  — True if data was decimated to fit max_points
          original_n   — number of samples in the requested window before decimation
    """
    result = _storage().get_waveform(
        session_id = session_id,
        channel    = channel,
        max_points = max_points,
        start_s    = start_s if start_s > 0 else None,
        end_s      = end_s   if end_s   > 0 else None,
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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == '__main__':
    main()
