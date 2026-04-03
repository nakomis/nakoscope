#!/usr/bin/env python3
"""Nakoscope capture CLI.

Commands:
  record   Start a recording session (Ctrl+C to stop).
  list     List recent sessions.
  info     Show details for a specific session.

Settings are read from ~/.nakoscope.yaml (CLI flags override config).
Requires root on macOS (libusb USB access):
  sudo python cli.py record
"""

import argparse
import os
import signal
import sys
import time
from pathlib import Path

# Ensure 'core' is importable from wherever the script is called
sys.path.insert(0, str(Path(__file__).parent))


def _cfg(key):
    """Read a config value (lazy-loads ~/.nakoscope.yaml)."""
    from core.config import get
    return get(key)


def _make_storage(args):
    from core.storage import create_backend
    return create_backend(backend=args.backend or None)


def cmd_record(args):
    from core.recorder import Recorder
    from core.devices.vds1022 import VDS1022Device

    device  = VDS1022Device()
    storage = _make_storage(args)

    print(f'Connecting to VDS1022...')
    device.connect()

    info = device.info()
    print(f'Connected: {info.name}  serial={info.serial}')

    device.configure(
        sample_rate        = args.sample_rate,
        channels           = args.channels,
        v_range            = args.v_range,
        coupling           = args.coupling,
        probe_attenuation  = args.probe,
    )
    print(f'Configured: channels={args.channels}  rate={args.sample_rate/1000:.0f}KS/s  '
          f'range=±{args.v_range/2:.1f}V  coupling={args.coupling}  probe=x{args.probe:.0f}')

    stop_requested = False

    def _on_frame(stats):
        elapsed = stats.elapsed_s
        rate    = stats.n_samples / elapsed if elapsed > 0 else 0
        print(
            f'\r  {elapsed:6.1f}s  '
            f'{stats.n_frames} frames  '
            f'{stats.n_samples:,} samples  '
            f'({rate/1000:.0f} KS/s)',
            end='', flush=True,
        )

    recorder = Recorder(device, storage, on_frame=_on_frame)

    def _stop(sig, frame):
        nonlocal stop_requested
        if not stop_requested:
            stop_requested = True
            print('\nStopping...', flush=True)
            recorder.stop()

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    session_id = recorder.start(notes=args.notes)
    print(f'Recording  session={session_id}')
    print(f'Press Ctrl+C to stop.\n')

    # Block until the recorder thread finishes
    recorder._thread.join()

    device.disconnect()

    stats = recorder.stats
    print(f'\nSaved  session={stats.session_id}')
    print(f'       {stats.n_frames} frames  {stats.n_samples:,} samples  {stats.elapsed_s:.1f}s')


def cmd_list(args):
    storage = _make_storage(args)
    sessions = storage.list_sessions(limit=args.limit, search=args.search)

    if not sessions:
        print('No sessions found.')
        return

    print(f'{"SESSION ID":<30}  {"STARTED":>20}  {"DUR":>6}  {"SAMPLES":>10}  NOTES')
    print('-' * 90)
    for s in sessions:
        dur  = f'{s["duration_s"]:.1f}s' if s['duration_s'] else '?'
        samp = f'{s["n_samples"]:,}'
        started = s['started_at'][:19].replace('T', ' ')
        print(f'{s["session_id"]:<30}  {started:>20}  {dur:>6}  {samp:>10}  {s["notes"]}')


def cmd_info(args):
    storage = _make_storage(args)
    s = storage.get_session(args.session_id)

    if s is None:
        print(f'Session not found: {args.session_id}')
        sys.exit(1)

    print(f'Session:  {s["session_id"]}')
    print(f'Device:   {s["device_name"]}  serial={s["device_serial"]}')
    print(f'Started:  {s["started_at"]}')
    print(f'Ended:    {s["ended_at"]}')
    print(f'Duration: {s["duration_s"]}s')
    print(f'Frames:   {s["n_frames"]}')
    print(f'Notes:    {s["notes"]}')
    print(f'Channels:')
    for ch, info in s['channels'].items():
        print(f'  {ch.upper():4s}  {info["n_samples"]:>10,} samples  '
              f'{info["sample_rate"]/1000:.0f} KS/s  '
              f'range=±{info["v_range"]/2:.1f}V')


def _add_backend_arg(p):
    p.add_argument(
        '--backend', default=None, choices=['hdf5', 's3'],
        help='Storage backend (default: s3 if NAKOSCOPE_S3_BUCKET is set, else hdf5)',
    )


def main():
    # Load config early so argparse defaults reflect it
    cap = _cfg('capture') or {}

    parser = argparse.ArgumentParser(
        description='Nakoscope capture tool',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = parser.add_subparsers(dest='command', required=True)

    # --- record ---
    p_rec = sub.add_parser('record', help='Start a recording session (Ctrl+C to stop)')
    p_rec.add_argument('--notes',       default='',  help='Free-text notes for this session')
    p_rec.add_argument('--channels',    default=cap.get('channels', ['CH1', 'CH2']),
                       nargs='+', help='Channels to capture')
    p_rec.add_argument('--sample-rate', default=cap.get('sample_rate', 250_000),
                       type=float, dest='sample_rate', help='Sample rate in Hz')
    p_rec.add_argument('--v-range',     default=cap.get('v_range', 10.0),
                       type=float, dest='v_range',
                       help='Full-scale voltage range across 10 divs (e.g. 10 = ±5V)')
    p_rec.add_argument('--coupling',    default=cap.get('coupling', 'DC'),
                       choices=['DC', 'AC'])
    p_rec.add_argument('--probe',       default=cap.get('probe', 1.0),
                       type=float, help='Probe attenuation (1 for x1, 10 for x10)')
    _add_backend_arg(p_rec)

    # --- list ---
    p_list = sub.add_parser('list', help='List recent sessions')
    p_list.add_argument('--limit',  default=20,   type=int)
    p_list.add_argument('--search', default=None, help='Filter by notes text')
    _add_backend_arg(p_list)

    # --- info ---
    p_info = sub.add_parser('info', help='Show details for a session')
    p_info.add_argument('session_id')
    _add_backend_arg(p_info)

    args = parser.parse_args()
    dispatch = {'record': cmd_record, 'list': cmd_list, 'info': cmd_info}
    dispatch[args.command](args)


if __name__ == '__main__':
    main()
