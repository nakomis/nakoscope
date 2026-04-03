"""HDF5-backed session storage for oscilloscope captures."""

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import h5py
import numpy as np


# HDF5 layout:
#   /sessions/<session_id>/
#       attrs:  started_at, ended_at, notes, device_name, device_serial,
#               sample_rate, v_range, channels, n_frames, n_samples
#       /ch1    float32 dataset (resizable), attrs: v_range, sample_rate
#       /ch2    float32 dataset (resizable), attrs: v_range, sample_rate
#
# Datasets are created with chunking and gzip compression.

CHUNK_SAMPLES = 50_000   # ~200 KB per chunk at float32
COMPRESSION   = 'gzip'
COMPRESS_OPTS = 4


class HDF5Storage:

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write API (used by Recorder)
    # ------------------------------------------------------------------

    def start_session(self, notes: str = '', device_name: str = '', device_serial: str = '') -> str:
        """Create a new session group and return the session_id."""
        session_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:6]

        with h5py.File(self.path, 'a') as f:
            grp = f.require_group('sessions').create_group(session_id)
            grp.attrs['started_at']    = datetime.now(timezone.utc).isoformat()
            grp.attrs['ended_at']      = ''
            grp.attrs['notes']         = notes
            grp.attrs['device_name']   = device_name
            grp.attrs['device_serial'] = device_serial
            grp.attrs['n_frames']      = 0
            grp.attrs['n_samples']     = 0

        return session_id

    def append_frame(self, session_id: str, channel_name: str, samples: np.ndarray,
                     sample_rate: float, v_range: float) -> None:
        """Append samples for one channel to an open session."""
        key = channel_name.lower()

        with h5py.File(self.path, 'a') as f:
            grp = f['sessions'][session_id]

            if key not in grp:
                ds = grp.create_dataset(
                    key,
                    shape=(0,),
                    maxshape=(None,),
                    dtype=np.float32,
                    chunks=(CHUNK_SAMPLES,),
                    compression=COMPRESSION,
                    compression_opts=COMPRESS_OPTS,
                )
                ds.attrs['sample_rate'] = sample_rate
                ds.attrs['v_range']     = v_range
            else:
                ds = grp[key]

            old_len = ds.shape[0]
            new_len = old_len + len(samples)
            ds.resize((new_len,))
            ds[old_len:new_len] = samples.astype(np.float32)

    def end_session(self, session_id: str, n_frames: int) -> None:
        """Finalise a session — record end time and frame/sample counts."""
        with h5py.File(self.path, 'a') as f:
            grp = f['sessions'][session_id]
            grp.attrs['ended_at'] = datetime.now(timezone.utc).isoformat()
            grp.attrs['n_frames'] = n_frames

            # Sum samples across all channel datasets
            total = sum(
                grp[k].shape[0]
                for k in grp.keys()
                if isinstance(grp[k], h5py.Dataset)
            )
            grp.attrs['n_samples'] = total

    # ------------------------------------------------------------------
    # Read API (used by MCP server and CLI)
    # ------------------------------------------------------------------

    def list_sessions(self, limit: int = 20, since: Optional[str] = None,
                      search: Optional[str] = None) -> list[dict]:
        """Return summary dicts for recent sessions, newest first."""
        if not self.path.exists():
            return []

        with h5py.File(self.path, 'r') as f:
            if 'sessions' not in f:
                return []

            sessions = []
            for sid, grp in f['sessions'].items():
                attrs = dict(grp.attrs)
                if since and attrs.get('started_at', '') < since:
                    continue
                if search and search.lower() not in attrs.get('notes', '').lower():
                    continue
                channels = [k for k in grp.keys() if isinstance(grp[k], h5py.Dataset)]
                sessions.append({
                    'session_id':    sid,
                    'started_at':    attrs.get('started_at', ''),
                    'ended_at':      attrs.get('ended_at', ''),
                    'notes':         attrs.get('notes', ''),
                    'device_name':   attrs.get('device_name', ''),
                    'device_serial': attrs.get('device_serial', ''),
                    'n_frames':      int(attrs.get('n_frames', 0)),
                    'n_samples':     int(attrs.get('n_samples', 0)),
                    'channels':      channels,
                    'duration_s':    self._duration(attrs),
                })

        sessions.sort(key=lambda s: s['started_at'], reverse=True)
        return sessions[:limit]

    def get_session(self, session_id: str) -> Optional[dict]:
        """Return full metadata for a session."""
        if not self.path.exists():
            return None

        with h5py.File(self.path, 'r') as f:
            if session_id not in f.get('sessions', {}):
                return None
            grp = f['sessions'][session_id]
            attrs = dict(grp.attrs)
            channels = {}
            for k in grp.keys():
                if isinstance(grp[k], h5py.Dataset):
                    ds = grp[k]
                    channels[k] = {
                        'n_samples':   ds.shape[0],
                        'sample_rate': float(ds.attrs.get('sample_rate', 0)),
                        'v_range':     float(ds.attrs.get('v_range', 0)),
                    }

        return {
            'session_id':    session_id,
            'started_at':    attrs.get('started_at', ''),
            'ended_at':      attrs.get('ended_at', ''),
            'notes':         attrs.get('notes', ''),
            'device_name':   attrs.get('device_name', ''),
            'device_serial': attrs.get('device_serial', ''),
            'n_frames':      int(attrs.get('n_frames', 0)),
            'duration_s':    self._duration(attrs),
            'channels':      channels,
        }

    def get_waveform(self, session_id: str, channel: str,
                     max_points: int = 5000,
                     start_s: Optional[float] = None,
                     end_s: Optional[float] = None) -> Optional[dict]:
        """Return waveform samples for one channel, downsampled if needed.

        Returns a dict with:
            samples         list of float voltages
            time_axis       list of float seconds (relative to session start)
            sample_rate     original Hz
            v_range         full-scale range (volts)
            downsampled     bool — True if samples were decimated
            original_n      int — number of samples before decimation
        """
        if not self.path.exists():
            return None

        key = channel.lower()

        with h5py.File(self.path, 'r') as f:
            grp = f.get(f'sessions/{session_id}')
            if grp is None or key not in grp:
                return None

            ds          = grp[key]
            sample_rate = float(ds.attrs.get('sample_rate', 0))
            v_range     = float(ds.attrs.get('v_range', 0))
            n_total     = ds.shape[0]

            # Slice to time window if requested
            i_start = int(start_s * sample_rate) if start_s is not None else 0
            i_end   = int(end_s   * sample_rate) if end_s   is not None else n_total
            i_start = max(0, min(i_start, n_total))
            i_end   = max(i_start, min(i_end, n_total))

            raw = ds[i_start:i_end]

        n_slice = len(raw)

        # Downsample by taking evenly-spaced points
        if n_slice > max_points:
            indices = np.round(np.linspace(0, n_slice - 1, max_points)).astype(int)
            samples = raw[indices]
            downsampled = True
        else:
            samples = raw
            indices = np.arange(n_slice)
            downsampled = False

        time_axis = ((i_start + indices) / sample_rate).tolist() if sample_rate else []

        return {
            'samples':     samples.tolist(),
            'time_axis':   time_axis,
            'sample_rate': sample_rate,
            'v_range':     v_range,
            'downsampled': downsampled,
            'original_n':  n_slice,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _duration(attrs: dict) -> Optional[float]:
        started = attrs.get('started_at', '')
        ended   = attrs.get('ended_at', '')
        if not started or not ended:
            return None
        try:
            from datetime import datetime
            fmt = '%Y-%m-%dT%H:%M:%S.%f%z'
            t0 = datetime.fromisoformat(started)
            t1 = datetime.fromisoformat(ended)
            return round((t1 - t0).total_seconds(), 3)
        except Exception:
            return None
