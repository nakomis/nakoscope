#!/usr/bin/env python3
"""Oscilloscope MCP Server.

Provides Claude with read access to the nakoscope HDF5 datastore so that
waveform captures can be directly analysed in conversation.

Tools:
  list_sessions      — browse recent recording sessions
  get_session_info   — full metadata for one session
  get_waveform       — voltage samples for a channel (downsampled if large)
"""

import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Locate the storage module — the app directory lives alongside this one.
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root / 'app'))

from core.storage import create_backend, StorageBackend

mcp = FastMCP('nakoscope')

_backend: StorageBackend | None = None


def _storage() -> StorageBackend:
    global _backend
    if _backend is None:
        _backend = create_backend()
    return _backend


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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == '__main__':
    main()
