"""S3 storage backend.

Each session is stored as two objects in S3:

  <prefix>sessions/<session_id>/data.h5     — full HDF5 waveform data
  <prefix>sessions/<session_id>/meta.json   — session metadata (for fast listing)

During capture, data is written to a local HDF5 file in the cache directory.
On end_session(), the file and its metadata are uploaded to S3.

Reads check the local cache first; if the session file is absent it is
downloaded from S3 before being opened.

AWS credentials are resolved by boto3 in the standard order:
  1. Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
  2. ~/.aws/credentials
  3. IAM instance role / ECS task role

Dependencies: boto3 (pip install boto3)
"""

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from ..storage import StorageBackend
from .hdf5 import (
    LocalHDF5Backend, _new_session_id, _session_summary, _session_detail,
    _time_slice, _downsample,
)

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError as e:
    raise ImportError(
        'boto3 is required for the S3 backend. Install it with:\n'
        '  pip install boto3'
    ) from e

import h5py


class S3Backend(StorageBackend):
    """StorageBackend that stores session HDF5 files in AWS S3.

    Writes go to a local cache first; uploads happen on end_session().
    Reads check the cache before downloading from S3.
    """

    def __init__(self, bucket: str, prefix: str = 'oscilloscope/', cache_dir=None):
        self.bucket = bucket
        self.prefix = prefix.rstrip('/') + '/'
        self.cache_dir = Path(cache_dir or Path.home() / '.cache' / 'oscilloscope')
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._s3 = boto3.client('s3')

    # ── Write ──────────────────────────────────────────────────────────────────

    def start_session(self, notes='', device_name='', device_serial='') -> str:
        session_id = _new_session_id()

        # Delegate to a local HDF5 backend for the in-progress capture
        local = self._local_backend(session_id)
        local.start_session(notes=notes, device_name=device_name, device_serial=device_serial)

        return session_id

    def append_frame(self, session_id, channel_name, samples, sample_rate, v_range):
        self._local_backend(session_id).append_frame(
            session_id, channel_name, samples, sample_rate, v_range,
        )

    def end_session(self, session_id, n_frames):
        local = self._local_backend(session_id)
        local.end_session(session_id, n_frames)

        local_h5 = self._local_h5_path(session_id)

        # Upload HDF5 data file
        data_key = self._data_key(session_id)
        self._s3.upload_file(str(local_h5), self.bucket, data_key)

        # Build and upload metadata JSON for fast listing
        meta = local.get_session(session_id)
        meta_key = self._meta_key(session_id)
        self._s3.put_object(
            Bucket      = self.bucket,
            Key         = meta_key,
            Body        = json.dumps(meta).encode(),
            ContentType = 'application/json',
        )

    # ── Read ───────────────────────────────────────────────────────────────────

    def list_sessions(self, limit=20, since=None, search=None) -> list[dict]:
        """List sessions by scanning meta.json objects in S3."""
        prefix = self.prefix + 'sessions/'
        paginator = self._s3.get_paginator('list_objects_v2')

        sessions = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if not key.endswith('/meta.json'):
                    continue
                try:
                    body = self._s3.get_object(Bucket=self.bucket, Key=key)['Body'].read()
                    meta = json.loads(body)
                except Exception:
                    continue

                if since and meta.get('started_at', '') < since:
                    continue
                if search and search.lower() not in meta.get('notes', '').lower():
                    continue
                sessions.append(meta)

        sessions.sort(key=lambda s: s.get('started_at', ''), reverse=True)
        return sessions[:limit]

    def get_session(self, session_id) -> Optional[dict]:
        meta_key = self._meta_key(session_id)
        try:
            body = self._s3.get_object(Bucket=self.bucket, Key=meta_key)['Body'].read()
            return json.loads(body)
        except ClientError as e:
            if e.response['Error']['Code'] in ('NoSuchKey', '404'):
                return None
            raise

    def get_waveform(self, session_id, channel, max_points=5000,
                     start_s=None, end_s=None) -> Optional[dict]:
        local_h5 = self._ensure_cached(session_id)
        if local_h5 is None:
            return None
        return self._local_backend(session_id).get_waveform(
            session_id, channel, max_points, start_s, end_s,
        )

    # ── Internals ──────────────────────────────────────────────────────────────

    def _local_h5_path(self, session_id: str) -> Path:
        return self.cache_dir / f'{session_id}.h5'

    def _local_backend(self, session_id: str) -> LocalHDF5Backend:
        return LocalHDF5Backend(self._local_h5_path(session_id))

    def _data_key(self, session_id: str) -> str:
        return f'{self.prefix}sessions/{session_id}/data.h5'

    def _meta_key(self, session_id: str) -> str:
        return f'{self.prefix}sessions/{session_id}/meta.json'

    def _ensure_cached(self, session_id: str) -> Optional[Path]:
        """Return the local HDF5 path, downloading from S3 if necessary."""
        local = self._local_h5_path(session_id)
        if local.exists():
            return local
        data_key = self._data_key(session_id)
        try:
            self._s3.download_file(self.bucket, data_key, str(local))
            return local
        except ClientError as e:
            if e.response['Error']['Code'] in ('NoSuchKey', '404'):
                return None
            raise

    def clear_cache(self, session_id: Optional[str] = None) -> None:
        """Delete cached local HDF5 files to free disk space.

        Args:
            session_id: If given, delete only that session's cache file.
                        If None, delete the entire cache directory.
        """
        if session_id:
            p = self._local_h5_path(session_id)
            p.unlink(missing_ok=True)
        else:
            shutil.rmtree(self.cache_dir, ignore_errors=True)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
