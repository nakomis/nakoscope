"""VDS1022 device plugin.

Wraps the florentbr OWON-VDS1022 Python API:
  https://github.com/florentbr/OWON-VDS1022/tree/master/api/python

The API must be installed or accessible. Install it once with:
  pip install -e ~/repos/florentbr/OWON-VDS1022/api/python

This plugin requires root/sudo on macOS due to libusb IOKit access.
Run the CLI with: sudo python cli.py record ...
"""

import os
import sys
from typing import Iterator

import numpy as np

from ..device import CaptureDevice, CaptureFrame, ChannelData, DeviceInfo

# Locate the florentbr API. Try the installed package first, then fall back
# to the sibling repo path.
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


# Map our string names to the library's channel constants
_CHANNEL_MAP = {'CH1': _vds.CH1, 'CH2': _vds.CH2}
_COUPLING_MAP = {'DC': _vds.DC, 'AC': _vds.AC}


class VDS1022Device(CaptureDevice):
    """CaptureDevice implementation for the OWON VDS1022(i).

    Example::

        device = VDS1022Device()
        device.configure(
            sample_rate=250_000,
            channels=['CH1', 'CH2'],
            v_range=10.0,
        )
        with device:
            recorder.start()
    """

    def __init__(self):
        self._dev = None
        self._channels: list[str] = ['CH1']
        self._v_range:   float = 10.0
        self._sample_rate: float = 250_000.0
        self._coupling:  str = 'DC'
        self._probe_attenuation: float = 1.0

    # ------------------------------------------------------------------
    # CaptureDevice interface
    # ------------------------------------------------------------------

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
        flash = self._dev.read_flash() if hasattr(self._dev, 'read_flash') else {}
        return DeviceInfo(
            name          = 'VDS1022',
            serial        = getattr(self._dev, 'serial', ''),
            firmware      = getattr(self._dev, 'version', ''),
            n_channels    = 2,
            max_sample_rate = 25_000_000.0,
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

        self._channels         = channels or ['CH1']
        self._v_range          = v_range
        self._sample_rate      = sample_rate
        self._coupling         = coupling
        self._probe_attenuation = probe_attenuation

        coupling_const = _COUPLING_MAP.get(coupling.upper(), _vds.DC)
        probe_str      = f'x{int(probe_attenuation)}'
        # Convert v_range (full 10-div span) to per-div for the API
        v_per_div = v_range / 10.0

        # Find the closest valid volt range
        valid = list(_vds.VOLT_RANGES)
        closest_range = min(valid, key=lambda r: abs(r - v_per_div))

        for ch_name in ['CH1', 'CH2']:
            ch_const = _CHANNEL_MAP[ch_name]
            enabled  = ch_name in self._channels
            if enabled:
                self._dev.set_channel(
                    ch_const,
                    coupling = coupling_const,
                    range    = closest_range,
                    probe    = probe_str,
                    on       = True,
                )
            else:
                self._dev.set_channel(ch_const, on=False)

        # Find the closest valid sampling rate
        valid_rates = list(_vds.SAMPLING_RATES)
        closest_rate = min(valid_rates, key=lambda r: abs(r - sample_rate))
        self._dev.set_sampling(closest_rate)
        self._sample_rate = closest_rate

    def frames(self) -> Iterator[CaptureFrame]:
        """Yield CaptureFrames continuously in roll mode."""
        if self._dev is None:
            raise RuntimeError('Not connected.')

        for raw_frames in self._dev.read_iter():
            channels = {}
            for raw_frame in raw_frames:
                if raw_frame is None:
                    continue
                # Convert raw int8 ADC samples to float32 voltages
                buf = np.frombuffer(raw_frame.buffer, np.int8).copy() \
                    if not isinstance(raw_frame.buffer, np.ndarray) \
                    else raw_frame.buffer.copy()
                voltages = (buf * raw_frame.sy + raw_frame.ty).astype(np.float32)
                ch_name  = raw_frame.name  # 'CH1' or 'CH2'
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
