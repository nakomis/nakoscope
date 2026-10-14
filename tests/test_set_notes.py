import io
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from botocore.exceptions import ClientError

from app.core.backends.hdf5 import LocalHDF5Backend
from app.core.backends.s3 import S3Backend

REPO_ROOT = Path(__file__).parent.parent


# ── HDF5 ──────────────────────────────────────────────────────────────────────

class TestHDF5SetNotes:
    def test_returns_old_notes_and_updates(self, session_with_data):
        backend, sid = session_with_data
        assert backend.set_notes(sid, "RC filter at 1kHz") == "test run"
        assert backend.get_session(sid)["notes"] == "RC filter at 1kHz"
        assert backend.list_sessions()[0]["notes"] == "RC filter at 1kHz"

    def test_other_fields_untouched(self, session_with_data):
        backend, sid = session_with_data
        before = backend.get_session(sid)
        wf_before = backend.get_waveform(sid, "CH1")
        backend.set_notes(sid, "new")
        after = backend.get_session(sid)
        assert {k: v for k, v in after.items() if k != "notes"} == \
               {k: v for k, v in before.items() if k != "notes"}
        assert backend.get_waveform(sid, "CH1") == wf_before

    def test_unknown_session_raises(self, session_with_data):
        backend, _ = session_with_data
        with pytest.raises(KeyError):
            backend.set_notes("no-such-session", "x")

    def test_missing_file_raises(self, tmp_path):
        backend = LocalHDF5Backend(tmp_path / "absent.h5")
        with pytest.raises(KeyError):
            backend.set_notes("anything", "x")
        assert not backend.path.exists()


# ── S3 (stubbed client) ───────────────────────────────────────────────────────

class _StubS3:
    """Minimal in-memory stand-in for the boto3 S3 client."""

    def __init__(self):
        self.objects = {}
        self.puts = []

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = Body
        self.puts.append(Key)

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def get_paginator(self, name):
        stub = self

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                keys = [k for (b, k) in stub.objects if b == Bucket and k.startswith(Prefix)]
                yield {"Contents": [{"Key": k} for k in keys]}

        return _Paginator()

    def upload_file(self, filename, Bucket, Key):
        self.put_object(Bucket, Key, Path(filename).read_bytes())


@pytest.fixture
def s3_backend(tmp_path):
    backend = S3Backend(bucket="test-bucket", prefix="nako/", cache_dir=tmp_path / "cache")
    backend._s3 = _StubS3()
    sid = backend.start_session(notes="original", device_name="VDS1022", device_serial="SN1")
    backend.append_frame(sid, "CH1", np.zeros(100, dtype=np.float32), 1e6, 5.0)
    backend.end_session(sid, n_frames=1)
    return backend, sid


class TestS3SetNotes:
    def test_returns_old_notes_and_updates(self, s3_backend):
        backend, sid = s3_backend
        assert backend.set_notes(sid, "retagged") == "original"
        assert backend.get_session(sid)["notes"] == "retagged"
        assert backend.list_sessions()[0]["notes"] == "retagged"

    def test_only_meta_json_rewritten(self, s3_backend):
        backend, sid = s3_backend
        stub = backend._s3
        data_key = backend._data_key(sid)
        data_before = stub.objects[("test-bucket", data_key)]
        meta_before = json.loads(stub.objects[("test-bucket", backend._meta_key(sid))])
        stub.puts.clear()

        backend.set_notes(sid, "retagged")

        assert stub.puts == [backend._meta_key(sid)]
        assert stub.objects[("test-bucket", data_key)] == data_before
        meta_after = json.loads(stub.objects[("test-bucket", backend._meta_key(sid))])
        assert meta_after == {**meta_before, "notes": "retagged"}

    def test_unknown_session_raises(self, s3_backend):
        backend, _ = s3_backend
        backend._s3.puts.clear()
        with pytest.raises(KeyError):
            backend.set_notes("no-such-session", "x")
        assert backend._s3.puts == []


# ── CLI ───────────────────────────────────────────────────────────────────────

def _run_cli(tmp_path, *args):
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "NAKOSCOPE_BACKEND": "hdf5",
        "NAKOSCOPE_DATA": str(tmp_path / "captures.h5"),
    }
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "app" / "cli.py"), *args],
        capture_output=True, text=True, env=env, check=False,
    )


class TestRetagCLI:
    def test_retag_prints_old_and_new(self, tmp_path):
        backend = LocalHDF5Backend(tmp_path / "captures.h5")
        sid = backend.start_session(notes="old label")
        backend.end_session(sid, 0)

        result = _run_cli(tmp_path, "retag", sid, "new label")

        assert result.returncode == 0, result.stderr
        assert "Old notes: old label" in result.stdout
        assert "New notes: new label" in result.stdout
        assert backend.get_session(sid)["notes"] == "new label"

        listing = _run_cli(tmp_path, "list")
        assert "new label" in listing.stdout
        assert "old label" not in listing.stdout

    def test_retag_unknown_session_fails(self, tmp_path):
        backend = LocalHDF5Backend(tmp_path / "captures.h5")
        sid = backend.start_session(notes="keep me")
        backend.end_session(sid, 0)

        result = _run_cli(tmp_path, "retag", "no-such-session", "x")

        assert result.returncode == 1
        assert "Session not found: no-such-session" in result.stderr
        assert backend.get_session(sid)["notes"] == "keep me"
