import numpy as np
import pytest

from app.core.backends.hdf5 import LocalHDF5Backend


@pytest.fixture
def backend(tmp_path):
    return LocalHDF5Backend(tmp_path / "test_captures.h5")


@pytest.fixture
def session_with_data(backend):
    sid = backend.start_session(notes="test run", device_name="VDS1022", device_serial="TEST001")
    samples = np.linspace(-1.0, 1.0, 1000, dtype=np.float32)
    backend.append_frame(sid, "CH1", samples, sample_rate=1_000_000.0, v_range=5.0)
    backend.append_frame(sid, "CH2", samples * 0.5, sample_rate=1_000_000.0, v_range=5.0)
    backend.end_session(sid, n_frames=1)
    return backend, sid
