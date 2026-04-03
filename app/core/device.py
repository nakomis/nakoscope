"""Abstract base class for nakoscope capture devices."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator
import numpy as np


@dataclass
class ChannelData:
    """Voltage samples for one channel in a single frame."""
    name: str                  # e.g. 'CH1', 'CH2'
    samples: np.ndarray        # float32 voltages
    sample_rate: float         # Hz
    v_range: float             # full-scale voltage range (volts, 10 divs)
    time_offset: float         # seconds from frame start to first sample


@dataclass
class CaptureFrame:
    """One frame of data from the scope (all active channels)."""
    clock: float               # monotonic time (seconds) — for sequencing frames
    channels: dict             # channel name -> ChannelData


@dataclass
class DeviceInfo:
    """Static information about the connected device."""
    name: str
    serial: str
    firmware: str
    n_channels: int
    max_sample_rate: float
    max_bandwidth: float


class CaptureDevice(ABC):
    """Abstract capture device interface.

    Implementations wrap a specific hardware API (e.g. VDS1022).
    The recorder calls connect(), configure(), then iterates frames().
    """

    @abstractmethod
    def connect(self) -> None:
        """Open the USB connection to the device."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the USB connection."""

    @abstractmethod
    def info(self) -> DeviceInfo:
        """Return static device information."""

    @abstractmethod
    def configure(
        self,
        sample_rate: float,
        channels: list[str],
        v_range: float,
        coupling: str = 'DC',
        probe_attenuation: float = 1.0,
    ) -> None:
        """Configure the device before starting capture.

        Args:
            sample_rate:        Desired sample rate in Hz.
            channels:           List of channel names to enable, e.g. ['CH1', 'CH2'].
            v_range:            Full-scale voltage range (volts across 10 divs).
            coupling:           'DC' or 'AC'.
            probe_attenuation:  Probe multiplier (1.0 for x1, 10.0 for x10).
        """

    @abstractmethod
    def frames(self) -> Iterator[CaptureFrame]:
        """Yield CaptureFrames continuously until disconnect() is called.

        Each frame contains all active channels with calibrated float32 samples.
        The generator should exit cleanly when the device is disconnected or
        when the underlying library signals a stop.
        """

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()
