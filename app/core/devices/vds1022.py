"""VDS1022 device plugin.

Uses a Rust capture binary (nakoscope-capture) for the hot USB read loop,
which sustains 1.25 MS/s where the Python API tops out at ~400 KS/s.

Falls back to the florentbr Python API if the Rust binary is not built.
Build it once with:
  cd ~/repos/nakomis/nakoscope/capture && cargo build --release

The florentbr API is still used for connect/configure/info:
  https://github.com/florentbr/OWON-VDS1022/tree/master/api/python
Install with:
  pip install -e ~/repos/florentbr/OWON-VDS1022/api/python

Requires root/sudo on macOS (libusb IOKit access).
"""

import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import numpy as np

from ..device import CaptureDevice, CaptureFrame, ChannelData, DeviceInfo

# ── Locate the florentbr API ──────────────────────────────────────────────────

try:
    import vds1022 as _vds_mod
except ImportError:
    _api_path = os.path.expanduser('~/repos/florentbr/OWON-VDS1022/api/python')
    if _api_path not in sys.path:
        sys.path.insert(0, _api_path)
    try:
        import vds1022 as _vds_mod
    except ImportError as e:
        raise ImportError(
            'Cannot import vds1022. Install it with:\n'
            '  pip install -e ~/repos/florentbr/OWON-VDS1022/api/python\n'
            f'Original error: {e}'
        ) from e

from vds1022 import vds1022 as _vds

_CHANNEL_MAP = {'CH1': _vds.CH1, 'CH2': _vds.CH2}
_COUPLING_MAP = {'DC': _vds.DC, 'AC': _vds.AC}

# ── Rust binary location ───────────────────────────────────────────────────────

_HERE = Path(__file__).parent
_RUST_BIN = _HERE.parent.parent.parent.parent / 'capture' / 'target' / 'release' / 'nakoscope-capture'

# Frame packet magic
_MAGIC = b'NSC\0'
_HEADER_FMT = struct.Struct('<4sBL')  # magic(4) + channel(u8) + n_samples(u32)
_HEADER_SIZE = _HEADER_FMT.size       # 9 bytes


class VDS1022Device(CaptureDevice):
    """CaptureDevice for the OWON VDS1022(i).

    Uses the Rust binary for the capture loop when available (sustains
    1.25 MS/s); falls back to the Python API otherwise (~400 KS/s limit).
    """

    def __init__(self):
        self._dev              = None
        self._channels:   list[str] = ['CH1']
        self._v_range:    float = 10.0
        self._sample_rate: float = 250_000.0
        self._coupling:   str = 'DC'
        self._probe_attenuation: float = 1.0

    # ── CaptureDevice interface ───────────────────────────────────────────────

    def connect(self) -> None:
        self._dev = _vds.VDS1022()

    def disconnect(self) -> None:
        if self._dev is not None:
            try:
                self._dev.stop()
            except Exception:
                pass
            self._dev = None

    def info(self) -> DeviceInfo:
        if self._dev is None:
            raise RuntimeError('Not connected.')
        return DeviceInfo(
            name            = 'VDS1022',
            serial          = getattr(self._dev, 'serial', ''),
            firmware        = getattr(self._dev, 'version', ''),
            n_channels      = 2,
            max_sample_rate = 100_000_000.0,
            max_bandwidth   = 25_000_000.0,
        )

    def configure(
        self,
        sample_rate: float = 250_000.0,
        channels: list[str] = None,
        v_range: float = 10.0,
        coupling: str = 'DC',
        probe_attenuation: float = 1.0,
    ) -> None:
        if self._dev is None:
            raise RuntimeError('Not connected — call connect() first.')

        self._channels          = channels or ['CH1']
        self._v_range           = v_range
        self._sample_rate       = sample_rate
        self._coupling          = coupling
        self._probe_attenuation = probe_attenuation

        coupling_const = _COUPLING_MAP.get(coupling.upper(), _vds.DC)
        probe_str      = f'x{int(probe_attenuation)}'
        v_per_div      = v_range / 10.0
        valid          = list(_vds.VOLT_RANGES)
        closest_range  = min(valid, key=lambda r: abs(r - v_per_div))

        for ch_name in ['CH1', 'CH2']:
            ch_const = _CHANNEL_MAP[ch_name]
            if ch_name in self._channels:
                self._dev.set_channel(
                    ch_const,
                    coupling = coupling_const,
                    range    = closest_range,
                    probe    = probe_str,
                )
            else:
                self._dev.on[ch_const] = False

        valid_rates = list(_vds.SAMPLING_RATES)
        closest_rate = min(valid_rates, key=lambda r: abs(r - sample_rate))
        self._dev.set_sampling(closest_rate)
        self._sample_rate = closest_rate

    def frames(self) -> Iterator[CaptureFrame]:
        """Yield CaptureFrames in roll mode.

        Uses the Rust binary when built; falls back to the Python API.
        """
        if self._dev is None:
            raise RuntimeError('Not connected.')

        if _RUST_BIN.exists():
            yield from self._frames_rust()
        else:
            print(
                f'  [nakoscope] Rust binary not found at {_RUST_BIN}\n'
                f'  Run: cd capture && cargo build --release\n'
                f'  Falling back to Python API (max ~400 KS/s).',
                file=sys.stderr,
            )
            yield from self._frames_python()

    # ── Rust capture path ─────────────────────────────────────────────────────

    def _frames_rust(self) -> Iterator[CaptureFrame]:
        """Spawn nakoscope-capture and stream frames from its stdout."""
        ch_arg = 'both' if len(self._channels) > 1 else self._channels[0][-1]

        cmd = [
            str(_RUST_BIN),
            '--rate',     str(int(self._sample_rate)),
            '--channels', ch_arg,
            '--v-range',  str(self._v_range),
            '--coupling', self._coupling.lower(),
            '--probe',    str(int(self._probe_attenuation)),
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Read the connection event from stderr
        stderr_line = proc.stderr.readline()
        try:
            info = json.loads(stderr_line)
        except Exception:
            proc.kill()
            raise RuntimeError(f'nakoscope-capture failed to start: {stderr_line!r}')

        if info.get('event') == 'error':
            proc.kill()
            raise RuntimeError(f"nakoscope-capture error: {info.get('msg')}")

        sy       = float(info['sy'])
        ty       = float(info['ty'])
        act_rate = float(info['sample_rate'])

        stdout = proc.stdout
        buf    = b''

        try:
            while True:
                # Accumulate data until we have a full header
                while len(buf) < _HEADER_SIZE:
                    chunk = stdout.read(_HEADER_SIZE - len(buf))
                    if not chunk:
                        return
                    buf += chunk

                magic, channel, n_samples = _HEADER_FMT.unpack(buf[:_HEADER_SIZE])
                buf = buf[_HEADER_SIZE:]

                if magic != _MAGIC:
                    # Re-sync: scan for magic bytes
                    idx = buf.find(_MAGIC)
                    if idx >= 0:
                        buf = buf[idx:]
                    else:
                        buf = b''
                    continue

                # Read sample bytes
                while len(buf) < n_samples:
                    chunk = stdout.read(n_samples - len(buf))
                    if not chunk:
                        return
                    buf += chunk

                raw      = np.frombuffer(buf[:n_samples], dtype=np.int8).copy()
                buf      = buf[n_samples:]
                voltages = (raw.astype(np.float32) * sy + ty)
                ch_name  = f'CH{channel + 1}'

                yield CaptureFrame(
                    clock    = time.perf_counter(),
                    channels = {
                        ch_name: ChannelData(
                            name        = ch_name,
                            samples     = voltages,
                            sample_rate = act_rate,
                            v_range     = self._v_range,
                            time_offset = 0.0,
                        )
                    },
                )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ── Python fallback path ──────────────────────────────────────────────────

    def _frames_python(self) -> Iterator[CaptureFrame]:
        for raw_frames in self._dev.read_iter():
            channels = {}
            for raw_frame in raw_frames:
                if raw_frame is None:
                    continue
                buf = np.frombuffer(raw_frame.buffer, np.int8).copy() \
                    if not isinstance(raw_frame.buffer, np.ndarray) \
                    else raw_frame.buffer.copy()
                voltages = (buf * raw_frame.sy + raw_frame.ty).astype(np.float32)
                ch_name  = raw_frame.name
                channels[ch_name] = ChannelData(
                    name        = ch_name,
                    samples     = voltages,
                    sample_rate = self._sample_rate,
                    v_range     = self._v_range,
                    time_offset = raw_frame.tx,
                )
            yield CaptureFrame(
                clock    = raw_frames.clock,
                channels = channels,
            )
