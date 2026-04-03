"""Local HDF5 storage backend.

All sessions are stored as groups within a single HDF5 file:

  /sessions/<session_id>/
      attrs:  started_at, ended_at, notes, device_name, device_serial,
              n_frames, n_samples
      /ch1    float32 dataset (resizable, chunked, gzip-compressed)
      /ch2    float32 dataset (resizable, chunked, gzip-compressed)
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import uuid

import h5py
import numpy as np

from ..storage import StorageBackend

CHUNK_SAMPLES = 50_000
COMPRESSION   = 'gzip'
COMPRESS_OPTS = 4


class LocalHDF5Backend(StorageBackend):

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── Write ──────────────────────────────────────────────────────────────────

    def start_session(self, notes='', device_name='', device_serial='') -> str:
        session_id = _new_session_id()
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

    def append_frame(self, session_id, channel_name, samples, sample_rate, v_range):
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
            n = ds.shape[0]
            ds.resize((n + len(samples),))
            ds[n:] = samples.astype(np.float32)

    def end_session(self, session_id, n_frames):
        with h5py.File(self.path, 'a') as f:
            grp = f['sessions'][session_id]
            grp.attrs['ended_at'] = datetime.now(timezone.utc).isoformat()
            grp.attrs['n_frames'] = n_frames
            grp.attrs['n_samples'] = sum(
                grp[k].shape[0]
                for k in grp.keys()
                if isinstance(grp[k], h5py.Dataset)
            )

    # ── Read ───────────────────────────────────────────────────────────────────

    def list_sessions(self, limit=20, since=None, search=None):
        if not self.path.exists():
            return []
        sessions = []
        with h5py.File(self.path, 'r') as f:
            for sid, grp in f.get('sessions', {}).items():
                attrs = dict(grp.attrs)
                if since and attrs.get('started_at', '') < since:
                    continue
                if search and search.lower() not in attrs.get('notes', '').lower():
                    continue
                channels = [k for k in grp if isinstance(grp[k], h5py.Dataset)]
                sessions.append(_session_summary(sid, attrs, channels))
        sessions.sort(key=lambda s: s['started_at'], reverse=True)
        return sessions[:limit]

    def get_session(self, session_id):
        if not self.path.exists():
            return None
        with h5py.File(self.path, 'r') as f:
            grp = f.get(f'sessions/{session_id}')
            if grp is None:
                return None
            attrs    = dict(grp.attrs)
            channels = {
                k: {
                    'n_samples':   grp[k].shape[0],
                    'sample_rate': float(grp[k].attrs.get('sample_rate', 0)),
                    'v_range':     float(grp[k].attrs.get('v_range', 0)),
                }
                for k in grp
                if isinstance(grp[k], h5py.Dataset)
            }
        return _session_detail(session_id, attrs, channels)

    def get_waveform(self, session_id, channel, max_points=5000, start_s=None, end_s=None):
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
            i0, i1      = _time_slice(n_total, sample_rate, start_s, end_s)
            raw         = ds[i0:i1]
        return _downsample(raw, i0, sample_rate, v_range, max_points)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _new_session_id() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:6]


def _duration(attrs: dict) -> Optional[float]:
    try:
        t0 = datetime.fromisoformat(attrs['started_at'])
        t1 = datetime.fromisoformat(attrs['ended_at'])
        return round((t1 - t0).total_seconds(), 3)
    except Exception:
        return None


def _session_summary(sid, attrs, channels) -> dict:
    return {
        'session_id':    sid,
        'started_at':    attrs.get('started_at', ''),
        'ended_at':      attrs.get('ended_at', ''),
        'notes':         attrs.get('notes', ''),
        'device_name':   attrs.get('device_name', ''),
        'device_serial': attrs.get('device_serial', ''),
        'n_frames':      int(attrs.get('n_frames', 0)),
        'n_samples':     int(attrs.get('n_samples', 0)),
        'channels':      channels,
        'duration_s':    _duration(attrs),
    }


def _session_detail(sid, attrs, channels) -> dict:
    return {
        'session_id':    sid,
        'started_at':    attrs.get('started_at', ''),
        'ended_at':      attrs.get('ended_at', ''),
        'notes':         attrs.get('notes', ''),
        'device_name':   attrs.get('device_name', ''),
        'device_serial': attrs.get('device_serial', ''),
        'n_frames':      int(attrs.get('n_frames', 0)),
        'duration_s':    _duration(attrs),
        'channels':      channels,
    }


def _time_slice(n_total, sample_rate, start_s, end_s):
    i0 = int(start_s * sample_rate) if start_s is not None else 0
    i1 = int(end_s   * sample_rate) if end_s   is not None else n_total
    return max(0, min(i0, n_total)), max(0, min(i1, n_total))


def _downsample(raw, i_offset, sample_rate, v_range, max_points) -> dict:
    n = len(raw)
    if n > max_points:
        indices     = np.round(np.linspace(0, n - 1, max_points)).astype(int)
        samples     = raw[indices]
        downsampled = True
    else:
        indices     = np.arange(n)
        samples     = raw
        downsampled = False

    time_axis = ((i_offset + indices) / sample_rate).tolist() if sample_rate else []

    return {
        'samples':     samples.tolist(),
        'time_axis':   time_axis,
        'sample_rate': sample_rate,
        'v_range':     v_range,
        'downsampled': downsampled,
        'original_n':  n,
    }
