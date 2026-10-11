import threading
import time

import numpy as np
import pytest

from app.core.device import CaptureFrame, ChannelData
from app.core.triggered import (
    TriggerSettings,
    fetch_with_timeout,
    run_triggered,
    save_capture,
)


class TestTriggerSettings:
    def test_defaults_are_valid(self):
        s = TriggerSettings()
        assert s.source == 'CH1'
        assert s.channels == ['CH1', 'CH2']

    def test_normalises_case(self):
        s = TriggerSettings(source='ext', edge='FALL', coupling='dc', channels=['ch1'])
        assert (s.source, s.edge, s.coupling, s.channels) == ('EXT', 'fall', 'DC', ['CH1'])

    @pytest.mark.parametrize('kwargs', [
        dict(source='CH3'),
        dict(edge='both'),
        dict(position=1.5),
        dict(offset=-0.1),
        dict(channels=[]),
        dict(channels=['CH1', 'EXT']),
        dict(timeout=0),
        dict(sample_rate=-1),
        dict(source='CH2', channels=['CH1']),
    ])
    def test_rejects_bad_settings(self, kwargs):
        with pytest.raises(ValueError):
            TriggerSettings(**kwargs)

    def test_ext_source_needs_no_matching_channel(self):
        TriggerSettings(source='EXT', channels=['CH1'])

    def test_metadata_records_mode_and_settings(self):
        meta = TriggerSettings(source='EXT', level=1.5, position=0.2).to_metadata()
        assert meta['mode'] == 'triggered'
        assert meta['source'] == 'EXT'
        assert meta['level'] == 1.5
        assert meta['position'] == 0.2


class TestFetchWithTimeout:
    def test_returns_result_when_fetch_completes(self):
        stopped = []
        assert fetch_with_timeout(lambda: 'frames', lambda: stopped.append(1), 5) == 'frames'
        assert stopped == []

    def test_stops_and_returns_none_on_timeout(self):
        stop = threading.Event()

        def fetch():
            stop.wait(5)
            # florentbr's fetch_iter returns once stopped

        t0 = time.monotonic()
        assert fetch_with_timeout(fetch, stop.set, 0.1) is None
        assert time.monotonic() - t0 < 2

    def test_stop_iteration_means_no_capture(self):
        def fetch():
            raise StopIteration

        assert fetch_with_timeout(fetch, lambda: None, 5) is None


def _frame(t0=-25e-6):
    samples = np.linspace(0, 1.5, 5000, dtype=np.float32)
    return CaptureFrame(clock=0.0, channels={
        name: ChannelData(name=name, samples=samples, sample_rate=100e6,
                          v_range=2.0, time_offset=t0)
        for name in ('CH1', 'CH2')
    })


class TestSaveCapture:
    def test_session_round_trip(self, backend):
        settings = TriggerSettings(source='EXT', level=1.5, position=0.25)
        sid = save_capture(backend, _frame(), settings, notes='READ burst',
                           device_name='VDS1022', device_serial='SN1')

        session = backend.get_session(sid)
        assert session['notes'] == 'READ burst'
        assert session['n_frames'] == 1
        assert session['metadata']['mode'] == 'triggered'
        assert session['metadata']['source'] == 'EXT'
        assert session['channels']['ch1']['time_offset'] == pytest.approx(-25e-6)

        summary = backend.list_sessions()[0]
        assert summary['metadata']['mode'] == 'triggered'

    def test_time_axis_is_trigger_relative(self, backend):
        sid = save_capture(backend, _frame(t0=-25e-6), TriggerSettings())
        wf = backend.get_waveform(sid, 'CH1', max_points=10_000)
        assert wf['time_axis'][0] == pytest.approx(-25e-6)
        assert wf['time_axis'][2500] == pytest.approx(0.0, abs=1e-12)
        assert wf['time_offset'] == pytest.approx(-25e-6)

    def test_time_window_is_trigger_relative(self, backend):
        sid = save_capture(backend, _frame(t0=-25e-6), TriggerSettings())
        wf = backend.get_waveform(sid, 'CH1', max_points=10_000, start_s=0.0, end_s=1e-6)
        assert wf['original_n'] == 100
        assert wf['time_axis'][0] == pytest.approx(0.0, abs=1e-12)


class TestRollSessionsUnchanged:
    def test_no_metadata_and_zero_offset(self, session_with_data):
        backend, sid = session_with_data
        session = backend.get_session(sid)
        assert session['metadata'] == {}
        assert session['channels']['ch1']['time_offset'] == 0.0
        wf = backend.get_waveform(sid, 'CH1')
        assert wf['time_axis'][0] == 0.0


class FakeScope:
    """Stands in for VDS1022Device: records calls, returns a canned frame."""

    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def connect(self):
        self.calls.append('connect')

    def disconnect(self):
        self.calls.append('disconnect')

    def info(self):
        from app.core.device import DeviceInfo
        return DeviceInfo('VDS1022', 'SN9', '1.0', 2, 100e6, 25e6)

    def capture_triggered(self, settings):
        self.calls.append(('capture', settings.source))
        return self.frame


class TestRunTriggered:
    def test_stores_session_and_disconnects(self, backend):
        scope = FakeScope(_frame())
        sid = run_triggered(scope, backend, TriggerSettings(source='EXT'), notes='hi')
        assert scope.calls == ['connect', ('capture', 'EXT'), 'disconnect']
        session = backend.get_session(sid)
        assert session['device_serial'] == 'SN9'
        assert session['notes'] == 'hi'

    def test_timeout_creates_no_session(self, backend):
        scope = FakeScope(None)
        assert run_triggered(scope, backend, TriggerSettings()) is None
        assert backend.list_sessions() == []
        assert scope.calls[-1] == 'disconnect'

    def test_disconnects_on_error(self, backend):
        scope = FakeScope(_frame())
        scope.capture_triggered = lambda s: (_ for _ in ()).throw(RuntimeError('usb'))
        with pytest.raises(RuntimeError):
            run_triggered(scope, backend, TriggerSettings())
        assert scope.calls[-1] == 'disconnect'
