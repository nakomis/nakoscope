import pytest

from app.core.backends.hdf5 import LocalHDF5Backend
from app.core.storage import create_backend


def test_create_hdf5_backend_explicit(tmp_path):
    backend = create_backend("hdf5", path=tmp_path / "test.h5")
    assert isinstance(backend, LocalHDF5Backend)


def test_create_hdf5_backend_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NAKOSCOPE_BACKEND", "hdf5")
    monkeypatch.setenv("NAKOSCOPE_DATA", str(tmp_path / "env.h5"))
    backend = create_backend()
    assert isinstance(backend, LocalHDF5Backend)


def test_unknown_backend_raises(tmp_path):
    with pytest.raises(ValueError, match="Unknown storage backend"):
        create_backend("ftp")


def test_s3_backend_without_bucket_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("NAKOSCOPE_S3_BUCKET", raising=False)
    monkeypatch.delenv("NAKOSCOPE_BACKEND", raising=False)
    import app.core.config as cfg_module
    monkeypatch.setattr(cfg_module, "CONFIG_PATH", tmp_path / "no-config.yaml")
    monkeypatch.setattr(cfg_module, "_cache", None)
    with pytest.raises(ValueError, match="bucket"):
        create_backend("s3")
