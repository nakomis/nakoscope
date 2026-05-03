# nakoscope

Oscilloscope data capture, storage, and analysis tool. Records waveform data from a USB oscilloscope (OWON VDS1022/VDS1022i), stores captures in HDF5 format, and exposes them to Claude via an MCP server.

Taiga project prefix: **SCOPE** (see project-prefixes note below)

## Architecture

```
VDS1022 (USB) → app/core/recorder.py → Storage backend
                                        ├── app/core/backends/hdf5.py  (local)
                                        └── app/core/backends/s3.py    (AWS S3)

Claude / MCP → mcp/server.py → Storage backend (read-only)
```

- **`app/`** — Python library and CLI (`nakoscope record|list|info`)
- **`app/core/device.py`** — Abstract `CaptureDevice` base class; add new oscilloscope plugins here
- **`app/core/devices/vds1022.py`** — OWON VDS1022 driver (uses the florentbr Python API)
- **`app/core/backends/`** — `hdf5.py` (local file), `s3.py` (AWS S3)
- **`app/core/recorder.py`** — Threaded capture loop
- **`mcp/server.py`** — MCP server exposing stored captures to Claude

## Primary use case

Debugging circuits. The typical workflow:

1. Connect the oscilloscope to the circuit under test
2. Run `nakoscope record` to capture waveforms into HDF5
3. In a Claude session, use the `nakoscope` MCP (available via `meta-mcp`) to pull captures
4. Ask Claude to help interpret the traces — signal integrity, timing, unexpected behaviour

The `nakoscope` MCP is registered in `meta-mcp/config.toml` and is available via `call_mcp("nakoscope", ...)`.

## Storage

Waveforms are stored in **HDF5** format (`.h5` files). Each capture is a group containing:
- Channel data arrays (voltage vs. time)
- Metadata: sample rate, timebase, trigger settings, timestamp

The `data/` directory contains scripts for copying captures to S3. A future NAS backend (Luke's home server — currently running badblocks disk tests) will replace or supplement S3.

## Dependencies

`app/` requires: `h5py`, `numpy`, `pyyaml`, `pyusb`, plus the florentbr VDS1022 Python API installed separately:

```
pip install -e ~/repos/florentbr/OWON-VDS1022/api/python
```

`mcp/` uses `pyproject.toml` (hatchling). Install with `pip install -e mcp/`.

## Testing

Tests live in `tests/`. Run with `pytest`. The test suite covers the core library; hardware-dependent device code is excluded.

## Project management

Taiga project prefix: **SCOPE** — story refs look like `SCOPE-1`, `SCOPE-2`, etc.

Project prefixes are tracked in:
`/Users/nakomis/repos/nakomis/home-servers/taiga/docs/project-prefixes.md`

When adding a new Taiga project, pick an unused prefix, add it **alphabetically** to that file, then run:
```
~/scripts/md-to-pdf /Users/nakomis/repos/nakomis/home-servers/taiga/docs/project-prefixes.md
```
