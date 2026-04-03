"""StorageBackend ABC and factory.

Available backends:
  hdf5  — single local HDF5 file, all sessions as groups (default)
  s3    — one HDF5 file per session in S3, with a local cache

Configuration via environment variables or keyword arguments to create_backend():

  hdf5:
    OSCILLOSCOPE_DATA   path to the .h5 file
                        (default: <repo>/data/captures.h5)

  s3:
    OSCILLOSCOPE_S3_BUCKET   S3 bucket name
    OSCILLOSCOPE_S3_PREFIX   key prefix (default: 'oscilloscope/')
    OSCILLOSCOPE_S3_CACHE    local cache directory
                             (default: ~/.cache/oscilloscope)
    AWS credentials via the standard boto3 chain (env vars, ~/.aws, IAM role, etc.)
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class StorageBackend(ABC):
    """Abstract interface for oscilloscope session storage."""

    # ── Write API (called by Recorder) ────────────────────────────────────────

    @abstractmethod
    def start_session(
        self,
        notes: str = '',
        device_name: str = '',
        device_serial: str = '',
    ) -> str:
        """Create a new session and return its session_id."""

    @abstractmethod
    def append_frame(
        self,
        session_id: str,
        channel_name: str,
        samples,           # numpy float32 array
        sample_rate: float,
        v_range: float,
    ) -> None:
        """Append samples for one channel to an open session."""

    @abstractmethod
    def end_session(self, session_id: str, n_frames: int) -> None:
        """Finalise a session. Implementations may flush/upload here."""

    # ── Read API (called by MCP server and CLI) ───────────────────────────────

    @abstractmethod
    def list_sessions(
        self,
        limit: int = 20,
        since: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[dict]:
        """Return summary dicts for recent sessions, newest first."""

    @abstractmethod
    def get_session(self, session_id: str) -> Optional[dict]:
        """Return full metadata for a session, or None if not found."""

    @abstractmethod
    def get_waveform(
        self,
        session_id: str,
        channel: str,
        max_points: int = 5000,
        start_s: Optional[float] = None,
        end_s: Optional[float] = None,
    ) -> Optional[dict]:
        """Return waveform samples for one channel, downsampled if needed."""


# ── Factory ───────────────────────────────────────────────────────────────────

def create_backend(backend: Optional[str] = None, **kwargs) -> StorageBackend:
    """Instantiate and return a StorageBackend.

    Args:
        backend:  'hdf5' or 's3'. If None, inferred from environment:
                  uses 's3' when OSCILLOSCOPE_S3_BUCKET is set, else 'hdf5'.
        **kwargs: Backend-specific options (override environment variables).
                  hdf5: path (str|Path)
                  s3:   bucket (str), prefix (str), cache_dir (str|Path)
    """
    if backend is None:
        backend = 's3' if os.environ.get('OSCILLOSCOPE_S3_BUCKET') else 'hdf5'

    if backend == 'hdf5':
        from .backends.hdf5 import LocalHDF5Backend
        path = kwargs.get('path') or os.environ.get(
            'OSCILLOSCOPE_DATA',
            Path(__file__).parent.parent.parent / 'data' / 'captures.h5',
        )
        return LocalHDF5Backend(path)

    if backend == 's3':
        from .backends.s3 import S3Backend
        bucket = kwargs.get('bucket') or os.environ.get('OSCILLOSCOPE_S3_BUCKET')
        if not bucket:
            raise ValueError(
                'S3 backend requires OSCILLOSCOPE_S3_BUCKET to be set '
                '(or pass bucket= to create_backend).'
            )
        prefix    = kwargs.get('prefix')    or os.environ.get('OSCILLOSCOPE_S3_PREFIX', 'oscilloscope/')
        cache_dir = kwargs.get('cache_dir') or os.environ.get(
            'OSCILLOSCOPE_S3_CACHE',
            Path.home() / '.cache' / 'oscilloscope',
        )
        return S3Backend(bucket=bucket, prefix=prefix, cache_dir=cache_dir)

    raise ValueError(f"Unknown storage backend: {backend!r}. Choose 'hdf5' or 's3'.")
