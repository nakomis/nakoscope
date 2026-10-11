import pytest

from app.cli import build_parser, settings_from_args


def _parse(*argv):
    return build_parser({}).parse_args(['capture', *argv])


def test_defaults():
    s = settings_from_args(_parse())
    assert (s.source, s.edge, s.sample_rate, s.channels) == ('CH1', 'rise', 100e6, ['CH1', 'CH2'])


def test_ddr_style_capture():
    s = settings_from_args(_parse(
        '--trigger', 'ext', '--edge', 'FALL', '--position', '0.2',
        '--sample-rate', '50e6', '--v-range', '2', '--offset', '0.1',
        '--channels', 'CH1', 'CH2', '--timeout', '3', '--probe', '10',
    ))
    assert s.source == 'EXT'
    assert s.edge == 'fall'
    assert s.position == 0.2
    assert s.sample_rate == 50e6
    assert s.v_range == 2.0
    assert s.offset == 0.1
    assert s.timeout == 3.0
    assert s.probe_attenuation == 10.0


def test_bad_source_rejected_by_argparse():
    with pytest.raises(SystemExit):
        _parse('--trigger', 'CH3')


def test_invalid_combination_raises():
    with pytest.raises(ValueError):
        settings_from_args(_parse('--trigger', 'CH2', '--channels', 'CH1'))
