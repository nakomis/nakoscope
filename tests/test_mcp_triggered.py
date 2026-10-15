"""Arm/wait/cancel lifecycle of the MCP triggered-capture tools, with a fake scope."""

import importlib.util
import sys
import threading
import types
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip('mcp.server.fastmcp')

from app.core.device import CaptureFrame, ChannelData, DeviceInfo

_SERVER = Path(__file__).parent.parent / 'mcp' / 'server.py'


class FakeScope:
    """Stands in for VDS1022Device. Behaviour is set per test via class attributes."""
    behaviour = 'capture'  # 'capture' | 'timeout' | 'error' | 'block'
    release = threading.Event()

    def connect(self): pass

    def disconnect(self): pass

    def info(self):
        return DeviceInfo('VDS1022', 'FAKE', '1.0', 2, 100e6, 25e6)

    def abort(self):
        FakeScope.release.set()

    def capture_triggered(self, settings):
        if self.behaviour == 'error':
            raise ValueError('Scope rejected the settings')
        if self.behaviour == 'timeout':
            return None
        if self.behaviour == 'block':
            FakeScope.release.wait(5)
            return None
        v = np.concatenate([np.zeros(2500), np.full(2500, 1.5)]).astype(np.float32)
        return CaptureFrame(clock=0.0, channels={
            'CH1': ChannelData('CH1', v, 100e6, settings.v_range, -25e-6),
        })


@pytest.fixture
def server(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('nakoscope_server', _SERVER)
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)

    fake = types.ModuleType('core.devices.vds1022')
    fake.VDS1022Device = FakeScope
    monkeypatch.setitem(sys.modules, 'core.devices.vds1022', fake)

    from core.backends.hdf5 import LocalHDF5Backend
    backend = LocalHDF5Backend(tmp_path / 'mcp.h5')
    monkeypatch.setattr(srv, '_storage', lambda: backend)

    FakeScope.behaviour = 'capture'
    FakeScope.release = threading.Event()
    return srv


ARGS = {'channels': ['CH1'], 'v_range': 2.5, 'offset': 0.2, 'level': 0.75}


def _lock_free(srv):
    if srv._capture_lock.acquire(blocking=False):
        srv._capture_lock.release()
        return True
    return False


def test_arm_then_wait_captures(server):
    assert server.arm_triggered(**ARGS)['armed']
    result = server.wait_triggered(5)
    assert result['status'] == 'captured'
    ch1 = result['levels']['ch1']
    assert ch1['max'] == pytest.approx(1.5)
    assert ch1['clipped'] is False
    assert _lock_free(server)


def test_clipping_is_reported(server):
    server.arm_triggered(channels=['CH1'], v_range=2.0, offset=0.5, level=0.5)
    assert server.wait_triggered(5)['levels']['ch1']['clipped'] is True


def test_timeout_saves_nothing(server):
    FakeScope.behaviour = 'timeout'
    server.arm_triggered(**ARGS)
    assert server.wait_triggered(5) == {'status': 'timeout', 'session_id': None}
    assert server.list_sessions() == []
    assert _lock_free(server)


def test_device_error_is_reported_and_lock_released(server):
    FakeScope.behaviour = 'error'
    server.arm_triggered(**ARGS)
    with pytest.raises(RuntimeError, match='rejected'):
        server.wait_triggered(5)
    assert _lock_free(server)


def test_uncollected_result_blocks_rearm(server):
    server.arm_triggered(**ARGS)
    server._armed['done'].wait(5)
    with pytest.raises(RuntimeError, match='not been collected'):
        server.arm_triggered(**ARGS)
    assert server.wait_triggered(5)['status'] == 'captured'
    server.arm_triggered(**ARGS)
    assert server.wait_triggered(5)['status'] == 'captured'


def test_busy_while_armed_and_cancel(server):
    FakeScope.behaviour = 'block'
    server.arm_triggered(**ARGS)
    with pytest.raises(RuntimeError, match='busy'):
        server.arm_triggered(**ARGS)
    with pytest.raises(RuntimeError, match='busy'):
        server.stop_recording()
    assert server.wait_triggered(0.05)['status'] == 'waiting'
    assert server.cancel_triggered()['status'] == 'timeout'
    assert _lock_free(server)


def test_bad_settings_fail_before_arming(server):
    with pytest.raises(ValueError):
        server.arm_triggered(channels=['CH1'], v_range=2.0, offset=0.5, level=1.5)
    assert server._armed is None
    assert _lock_free(server)


def test_missing_driver_fails_before_taking_the_lock(server, monkeypatch):
    monkeypatch.setitem(sys.modules, 'core.devices.vds1022', None)
    with pytest.raises(ImportError):
        server.arm_triggered(**ARGS)
    assert _lock_free(server)
