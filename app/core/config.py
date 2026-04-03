"""Nakoscope configuration loader.

Settings are resolved in priority order (highest wins):
  1. CLI flags / explicit kwargs
  2. Environment variables  (NAKOSCOPE_*)
  3. ~/.nakoscope.yaml
  4. Built-in defaults

Example ~/.nakoscope.yaml
--------------------------
backend: hdf5          # hdf5 | s3  (omit to auto-detect)
device: vds1022        # device plugin

# HDF5 backend
data_path: ~/repos/nakomis/nakoscope/data/captures.h5

# S3 backend
s3:
  bucket: my-nakoscope-bucket
  prefix: nakoscope/
  cache_dir: ~/.cache/nakoscope
  aws_profile: nakom.is-sandbox   # omit to use default AWS credential chain

# Capture defaults (all overridable per-run via CLI flags)
capture:
  sample_rate: 250000
  channels: [CH1, CH2]
  v_range: 10.0
  coupling: DC
  probe: 1.0
"""

import os
from pathlib import Path
from typing import Any, Optional

CONFIG_PATH = Path.home() / '.nakoscope.yaml'

_REPO_ROOT = Path(__file__).parent.parent.parent

_DEFAULTS: dict = {
    'backend': None,
    'device': 'vds1022',
    'data_path': str(_REPO_ROOT / 'data' / 'captures.h5'),
    's3': {
        'bucket': None,
        'prefix': 'nakoscope/',
        'cache_dir': str(Path.home() / '.cache' / 'nakoscope'),
        'aws_profile': None,   # e.g. 'nakom.is-sandbox'
    },
    'capture': {
        'sample_rate': 250_000,
        'channels': ['CH1', 'CH2'],
        'v_range': 10.0,
        'coupling': 'DC',
        'probe': 1.0,
    },
}

_cache: Optional[dict] = None


def load(reload: bool = False) -> dict:
    """Load and return the merged configuration dict.

    The result is cached after the first call. Pass reload=True to force
    a re-read of the YAML file (useful in tests).
    """
    global _cache
    if _cache is not None and not reload:
        return _cache

    import copy
    cfg = copy.deepcopy(_DEFAULTS)

    if CONFIG_PATH.exists():
        try:
            import yaml
        except ImportError as e:
            raise ImportError(
                'pyyaml is required to read ~/.nakoscope.yaml. '
                'Install it with: pip install pyyaml'
            ) from e
        with open(CONFIG_PATH) as f:
            user = yaml.safe_load(f) or {}
        _deep_merge(cfg, user)

    # Expand ~ in path values
    for key in ('data_path',):
        if cfg.get(key):
            cfg[key] = str(Path(cfg[key]).expanduser())
    if cfg.get('s3', {}).get('cache_dir'):
        cfg['s3']['cache_dir'] = str(Path(cfg['s3']['cache_dir']).expanduser())

    _cache = cfg
    return cfg


def get(dotted_key: str, default: Any = None) -> Any:
    """Retrieve a config value using dot notation.

    Examples::

        config.get('backend')
        config.get('s3.bucket')
        config.get('capture.sample_rate')
    """
    parts = dotted_key.split('.')
    val: Any = load()
    for part in parts:
        if not isinstance(val, dict):
            return default
        val = val.get(part, default)
        if val is None:
            return default
    return val


def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base in-place, recursing into nested dicts."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
