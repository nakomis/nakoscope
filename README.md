# nakoscope

Oscilloscope data capture, storage, and analysis tool. Records waveform data from a USB oscilloscope, stores it in HDF5 (locally or on S3), and exposes it to Claude via an MCP server.

Currently supports the **OWON VDS1022/VDS1022i** oscilloscope.

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

- **`app/`** — Python library and CLI (`nakoscope record|list|info`)
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

`libusb` is installed automatically by `install.sh` via Homebrew.

## Installation

```bash
sudo bash install.sh
```

The installer:
1. Copies app files to `/usr/local/lib/nakoscope/` (root-owned — not user-editable)
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

# List recent sessions
nakoscope list
nakoscope list --limit 50 --search "RC filter"

# Show details for a session
nakoscope info 20240402-143021-abc123
```

## MCP integration

Once installed, Claude can query your oscilloscope data directly:

> "List my recent captures"  
> "Show me the waveform from session 20240402-143021"  
> "What's the peak voltage in channel 1 of that last recording?"

The MCP server exposes three tools: `list_sessions`, `get_session_info`, `get_waveform`.

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

