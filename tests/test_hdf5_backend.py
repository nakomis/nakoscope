import numpy as np
import pytest

from app.core.backends.hdf5 import LocalHDF5Backend, _downsample, _time_slice


class TestSessionLifecycle:
    def test_start_session_returns_id(self, backend):
        sid = backend.start_session()
        assert isinstance(sid, str)
        assert len(sid) > 0

    def test_start_session_custom_id(self, backend):
        sid = backend.start_session(session_id="custom-id-123")
        assert sid == "custom-id-123"

    def test_start_session_metadata(self, backend):
        sid = backend.start_session(notes="hello", device_name="VDS1022", device_serial="SN001")
        session = backend.get_session(sid)
        assert session["notes"] == "hello"
        assert session["device_name"] == "VDS1022"
        assert session["device_serial"] == "SN001"

    def test_end_session_sets_n_frames(self, backend):
        sid = backend.start_session()
        samples = np.zeros(100, dtype=np.float32)
        backend.append_frame(sid, "CH1", samples, 1e6, 5.0)
        backend.end_session(sid, n_frames=42)
        session = backend.get_session(sid)
        assert session["n_frames"] == 42

    def test_end_session_counts_samples(self, backend):
        sid = backend.start_session()
        samples = np.zeros(500, dtype=np.float32)
        backend.append_frame(sid, "CH1", samples, 1e6, 5.0)
        backend.end_session(sid, n_frames=1)
        session = backend.get_session(sid)
        assert session["n_samples"] == 500


class TestAppendFrame:
    def test_append_creates_channel(self, backend):
        sid = backend.start_session()
        samples = np.ones(200, dtype=np.float32)
        backend.append_frame(sid, "CH1", samples, 1e6, 5.0)
        session = backend.get_session(sid)
        assert "ch1" in session["channels"]

    def test_append_multiple_frames_accumulates(self, backend):
        sid = backend.start_session()
        chunk = np.zeros(100, dtype=np.float32)
        backend.append_frame(sid, "CH1", chunk, 1e6, 5.0)
        backend.append_frame(sid, "CH1", chunk, 1e6, 5.0)
        backend.end_session(sid, n_frames=2)
        wf = backend.get_waveform(sid, "CH1")
        assert wf["original_n"] == 200

    def test_channel_name_normalised_to_lowercase(self, backend):
        sid = backend.start_session()
        backend.append_frame(sid, "CH1", np.zeros(50, dtype=np.float32), 1e6, 5.0)
        session = backend.get_session(sid)
        assert "ch1" in session["channels"]
        assert "CH1" not in session["channels"]


class TestListSessions:
    def test_empty_store_returns_empty_list(self, backend):
        assert backend.list_sessions() == []

    def test_returns_completed_sessions(self, session_with_data):
        backend, sid = session_with_data
        sessions = backend.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == sid

    def test_search_filters_by_notes(self, backend):
        sid1 = backend.start_session(notes="capacitor test")
        backend.end_session(sid1, 0)
        sid2 = backend.start_session(notes="inductor test")
        backend.end_session(sid2, 0)
        results = backend.list_sessions(search="capacitor")
        assert len(results) == 1
        assert results[0]["session_id"] == sid1

    def test_limit_respected(self, backend):
        for i in range(5):
            sid = backend.start_session(session_id=f"sess-{i:02d}")
            backend.end_session(sid, 0)
        results = backend.list_sessions(limit=3)
        assert len(results) == 3


class TestGetSession:
    def test_returns_none_for_missing_session(self, backend):
        assert backend.get_session("does-not-exist") is None

    def test_returns_none_when_file_absent(self, tmp_path):
        backend = LocalHDF5Backend(tmp_path / "nonexistent.h5")
        assert backend.get_session("anything") is None

    def test_channel_metadata_present(self, session_with_data):
        backend, sid = session_with_data
        session = backend.get_session(sid)
        ch1 = session["channels"]["ch1"]
        assert ch1["sample_rate"] == pytest.approx(1_000_000.0)
        assert ch1["v_range"] == pytest.approx(5.0)
        assert ch1["n_samples"] == 1000


class TestGetWaveform:
    def test_returns_samples(self, session_with_data):
        backend, sid = session_with_data
        wf = backend.get_waveform(sid, "CH1")
        assert wf is not None
        assert len(wf["samples"]) > 0

    def test_returns_none_for_missing_channel(self, session_with_data):
        backend, sid = session_with_data
        assert backend.get_waveform(sid, "CH3") is None

    def test_returns_none_for_missing_session(self, backend):
        assert backend.get_waveform("no-such-session", "CH1") is None

    def test_downsampling_applied_when_max_points_exceeded(self, backend):
        sid = backend.start_session()
        samples = np.zeros(10_000, dtype=np.float32)
        backend.append_frame(sid, "CH1", samples, 1e6, 5.0)
        wf = backend.get_waveform(sid, "CH1", max_points=100)
        assert len(wf["samples"]) == 100
        assert wf["downsampled"] is True

    def test_no_downsampling_when_below_max_points(self, session_with_data):
        backend, sid = session_with_data
        wf = backend.get_waveform(sid, "CH1", max_points=5000)
        assert wf["downsampled"] is False
        assert wf["original_n"] == 1000

    def test_time_axis_length_matches_samples(self, session_with_data):
        backend, sid = session_with_data
        wf = backend.get_waveform(sid, "CH1")
        assert len(wf["time_axis"]) == len(wf["samples"])


class TestHelpers:
    def test_time_slice_full_range(self):
        assert _time_slice(1000, 1e6, None, None) == (0, 1000)

    def test_time_slice_clamped_to_bounds(self):
        i0, i1 = _time_slice(1000, 1e6, -1.0, 999.0)
        assert i0 == 0
        assert i1 == 1000

    def test_time_slice_start_only(self):
        i0, i1 = _time_slice(1000, 1000.0, 0.5, None)
        assert i0 == 500
        assert i1 == 1000

    def test_downsample_no_op_when_small(self):
        raw = np.arange(10, dtype=np.float32)
        result = _downsample(raw, 0, 1000.0, 5.0, max_points=100)
        assert result["downsampled"] is False
        assert result["original_n"] == 10

    def test_downsample_reduces_points(self):
        raw = np.arange(1000, dtype=np.float32)
        result = _downsample(raw, 0, 1e6, 5.0, max_points=50)
        assert len(result["samples"]) == 50
        assert result["downsampled"] is True
