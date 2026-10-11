# nakoscope

Oscilloscope data capture, storage, and analysis tool. Records waveform data from a USB oscilloscope, stores it in HDF5 (locally or on S3), and exposes it to Claude via an MCP server.

Currently supports the **OWON VDS1022/VDS1022i** oscilloscope.

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=nakoscope)

## Architecture

```
┌─────────────────┐    ┌──────────────┐    ┌────────────────────┐
│  VDS1022 (USB)  │───▶│   Recorder   │───▶│  Storage backend   │
└─────────────────┘    └──────────────┘    │  ┌──────────────┐  │
                                           │  │ Local HDF5   │  │
┌─────────────────┐    ┌──────────────┐    │  ├──────────────┤  │
│  Claude / MCP   │───▶│  MCP server  │───▶│  │     S3       │  │
└─────────────────┘    └──────────────┘    │  └──────────────┘  │
                                           └────────────────────┘
```

- **`app/`** — Python library and CLI (`nakoscope record|list|info|retag`)
- **`app/core/device.py`** — Abstract `CaptureDevice` base class; add new oscilloscope plugins here
- **`app/core/backends/`** — `hdf5.py` (local file) and `s3.py` (AWS S3)
- **`app/core/recorder.py`** — Threaded capture loop
- **`mcp/`** — MCP server for Claude integration

## Requirements

- macOS (tested on Apple Silicon)
- [Homebrew](https://brew.sh/)
- Python 3.11+ managed via [asdf](https://asdf-vm.com/)
- [OWON VDS1022 Python API](https://github.com/florentbr/OWON-VDS1022)
- For S3 backend: `boto3` and an AWS account
- Optional but recommended: a Rust toolchain via [rustup](https://rustup.rs/), installed as your normal user — `install.sh` uses it to build the high-speed capture binary

`libusb` is installed automatically by `install.sh` via Homebrew.

## Installation

```bash
sudo bash install.sh
```

The installer:
1. Copies app files to `/usr/local/lib/nakoscope/` (root-owned — not user-editable)
1. Builds the Rust capture binary with `cargo build --release` (as you, not root) if it is missing and cargo is available, then installs it
2. Creates `/usr/local/bin/nakoscope` wrapper (root-owned)
3. Writes `/etc/sudoers.d/nakoscope` — NOPASSWD grant for the wrapper only
4. Creates `/var/log/nakoscope/`
5. Installs Python dependencies as the real user
6. Creates `~/.nakoscope.yaml` config (if absent)
7. Registers the MCP server with Claude Code

The NOPASSWD sudoers grant points only to the root-owned wrapper, not to any user-editable file, so it cannot be used for privilege escalation.

To update after pulling new code:

```bash
sudo bash install.sh
```

### Uninstalling

```bash
sudo bash uninstall.sh
```

Removes all system files. Your config (`~/.nakoscope.yaml`) and captured data (`data/`) are left intact. To also remove the MCP server:

```bash
claude mcp remove nakoscope
```

## Configuration

Settings are read from `~/.nakoscope.yaml`. CLI flags always take precedence.

```yaml
backend: s3          # hdf5 | s3  (omit to auto-detect)
device: vds1022

# HDF5 backend (used when backend: hdf5)
data_path: ~/repos/nakomis/nakoscope/data/captures.h5

# S3 backend
s3:
  bucket: my-nakoscope-bucket
  prefix: nakoscope/
  cache_dir: ~/.cache/nakoscope
  aws_profile: my-aws-profile    # omit to use default credentials

# Capture defaults (all overridable per-run via CLI flags)
capture:
  sample_rate: 250000
  channels: [CH1, CH2]
  v_range: 10.0      # full-scale range in V (10 = ±5 V)
  coupling: DC
  probe: 1.0         # probe attenuation (1 = ×1, 10 = ×10)
```

**Priority:** CLI flags > environment variables > `~/.nakoscope.yaml` > built-in defaults

### Environment variable overrides

| Variable | Description |
|---|---|
| `NAKOSCOPE_BACKEND` | `hdf5` or `s3` |
| `NAKOSCOPE_DATA` | Path to local HDF5 file |
| `NAKOSCOPE_S3_BUCKET` | S3 bucket name |
| `NAKOSCOPE_S3_PREFIX` | S3 key prefix |
| `NAKOSCOPE_S3_CACHE` | Local cache directory for S3 downloads |
| `NAKOSCOPE_S3_PROFILE` | AWS named profile |

## Usage

No `sudo` needed — the wrapper handles elevation automatically.

```bash
# Start recording (Ctrl+C to stop)
nakoscope record --notes "testing RC filter at 1kHz"

# Override capture settings for this run
nakoscope record --channels CH1 --sample-rate 500000 --v-range 5.0

# One-shot triggered capture: arm, wait for one edge, store a single
# full-rate frame (~5000 samples per channel; 50 µs at 100 MS/s)
nakoscope capture --trigger EXT --edge rise --position 0.2 \
    --sample-rate 100e6 --v-range 2 --offset 0.1 --timeout 10 --notes "READ burst"

# List recent sessions
nakoscope list
nakoscope list --limit 50 --search "RC filter"

# Show details for a session
nakoscope info 20240402-143021-abc123

# Change a session's notes after recording (prints the old and new notes)
nakoscope retag 20240402-143021-abc123 "RC filter at 1kHz, 10k/100n"
```

`retag` rewrites the notes in whichever backend holds the session. On S3 only the session's `meta.json` is rewritten; the waveform data is never touched. `list`, `info` and the MCP server's `list_sessions` all show the new notes straight away.

## MCP integration

Once installed, Claude can query your oscilloscope data directly:

> "List my recent captures"  
> "Show me the waveform from session 20240402-143021"  
> "What's the peak voltage in channel 1 of that last recording?"

The MCP server exposes:

- **Reading:** `list_sessions`, `get_session_info`, `get_waveform`
- **Roll-mode recording:** `record_for`, `start_recording`, `stop_recording`
- **Triggered capture:** `capture_triggered` (blocks until the trigger fires), or `arm_triggered` then `wait_triggered` when something has to be fired after arming

### Triggered capture

Roll mode streams continuously at up to ~1.25 MS/s. Triggered capture instead arms an edge trigger and fetches one frame of ~5000 samples per channel at up to 100 MS/s — for looking at MHz activity around a single event. The trigger source can be `CH1`, `CH2` or `EXT` (the Multi port as a TTL trigger input), so a firmware "marker" GPIO can trigger the scope while both channels watch the signals of interest.

Triggered sessions store their trigger settings under `metadata` (`mode: triggered`), and their time axes are trigger-relative: t = 0 is the trigger and negative times are pre-trigger. `--v-range` is full-scale at the probe tip, and `--offset` sets where 0 V sits on screen (0 = bottom), e.g. `--v-range 2 --offset 0.1` shows −0.2 V to 1.8 V for 1.5 V logic.

## Adding a new oscilloscope

Subclass `CaptureDevice` in `app/core/device.py` and implement:

```python
class MyDevice(CaptureDevice):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def info(self) -> DeviceInfo: ...
    def configure(self, sample_rate, channels, v_range, coupling, probe_attenuation) -> None: ...
    def frames(self) -> Iterator[CaptureFrame]: ...
```

## S3 bucket layout

```
s3://bucket/prefix/
  sessions/
    <session_id>/
      data.h5       waveform data (HDF5)
      meta.json     session metadata (lightweight — used for listing)
```

`list_sessions` scans only `meta.json` files, so listing is fast even with many sessions.

## Credits

The VDS1022 USB protocol is derived from [florentbr/OWON-VDS1022](https://github.com/florentbr/OWON-VDS1022). The Python API from that project is used directly for device connection and configuration, and the Rust capture binary (`nakoscope-capture`) re-implements the same USB protocol for high-speed roll-mode capture. Many thanks to florentbr for the excellent reverse-engineering work.

If nakoscope is useful to you, consider donating to florentbr via their project page.

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=nakoscope)

