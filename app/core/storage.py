"""StorageBackend ABC and factory.

Available backends:
  hdf5  — single local HDF5 file, all sessions as groups (default)
  s3    — one HDF5 file per session in S3, with a local cache

Backend selection priority (highest wins):
  1. Explicit backend= kwarg to create_backend()
  2. NAKOSCOPE_BACKEND environment variable
  3. backend key in ~/.nakoscope.yaml
  4. Auto-detect: 's3' if a bucket is configured, else 'hdf5'

Environment variables (override config file):
  hdf5:
    NAKOSCOPE_DATA          path to the .h5 file
  s3:
    NAKOSCOPE_S3_BUCKET     S3 bucket name
    NAKOSCOPE_S3_PREFIX     key prefix (default: 'nakoscope/')
    NAKOSCOPE_S3_CACHE      local cache directory
    NAKOSCOPE_S3_PROFILE    AWS profile name (e.g. 'nakom.is-sandbox')
    Standard AWS env vars (AWS_ACCESS_KEY_ID, etc.) also honoured by boto3.
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class StorageBackend(ABC):
    """Abstract interface for nakoscope session storage."""

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
        backend:  'hdf5' or 's3'. If None, resolved from env/config/auto-detect.
        **kwargs: Backend-specific overrides.
                  hdf5: path (str|Path)
                  s3:   bucket, prefix, cache_dir, aws_profile (all str)
    """
    from .config import get as cfg

    # ── Resolve backend ────────────────────────────────────────────────────────
    if backend is None:
        backend = (
            os.environ.get('NAKOSCOPE_BACKEND')
            or cfg('backend')
            or None
        )
    if backend is None:
        # Auto-detect: use s3 if a bucket is configured anywhere
        bucket_hint = (
            kwargs.get('bucket')
            or os.environ.get('NAKOSCOPE_S3_BUCKET')
            or cfg('s3.bucket')
        )
        backend = 's3' if bucket_hint else 'hdf5'

    # ── HDF5 ───────────────────────────────────────────────────────────────────
    if backend == 'hdf5':
        from .backends.hdf5 import LocalHDF5Backend
        path = (
            kwargs.get('path')
            or os.environ.get('NAKOSCOPE_DATA')
            or cfg('data_path')
            or Path(__file__).parent.parent.parent / 'data' / 'captures.h5'
        )
        return LocalHDF5Backend(path)

    # ── S3 ─────────────────────────────────────────────────────────────────────
    if backend == 's3':
        from .backends.s3 import S3Backend

        bucket = (
            kwargs.get('bucket')
            or os.environ.get('NAKOSCOPE_S3_BUCKET')
            or cfg('s3.bucket')
        )
        if not bucket:
            raise ValueError(
                'S3 backend requires a bucket name. Set NAKOSCOPE_S3_BUCKET, '
                "add 's3.bucket' to ~/.nakoscope.yaml, or pass bucket= to create_backend()."
            )

        prefix = (
            kwargs.get('prefix')
            or os.environ.get('NAKOSCOPE_S3_PREFIX')
            or cfg('s3.prefix')
            or 'nakoscope/'
        )
        cache_dir = (
            kwargs.get('cache_dir')
            or os.environ.get('NAKOSCOPE_S3_CACHE')
            or cfg('s3.cache_dir')
            or Path.home() / '.cache' / 'nakoscope'
        )
        aws_profile = (
            kwargs.get('aws_profile')
            or os.environ.get('NAKOSCOPE_S3_PROFILE')
            or cfg('s3.aws_profile')
        )

        return S3Backend(
            bucket      = bucket,
            prefix      = prefix,
            cache_dir   = cache_dir,
            aws_profile = aws_profile,
        )

    raise ValueError(f"Unknown storage backend: {backend!r}. Choose 'hdf5' or 's3'.")
