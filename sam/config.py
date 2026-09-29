#!/usr/bin/env python3
"""SAM config module: load config, resolve SAM_HOME, initialize directory tree.

Spec source: final-specification-v3.md + reviews-phase-f-batch1.md (GLM-5.2 config.py spec)
"""

import json
import os
import shutil
from pathlib import Path


# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "defaults": {
        "model": "opencode/muse-spark-1.3-contributor-free",
        "max_restarts": 5,
        "max_depth": 4,
        "harness": "pi",
    },
    "security": {
        "inherit_env": True,
    },
}

CONFIG_FILENAME = "config.json"
REGISTRY_FILENAME = "registry.json"
LOCK_FILENAME = "registry.lock"
LOCKS_DIRNAME = "locks"
BIN_DIRNAME = "bin"
WRAPPER_FILENAME = "pi-wrapper"
HARNESS_WRAPPERS = {"pi": "pi-wrapper", "agy": "agy-wrapper"}
AGENTS_DIRNAME = "agents"
TASKS_DIRNAME = "tasks"
EVENTS_FILENAME = "events.log"

# Required keys for validation
REQUIRED_CONFIG_KEYS = {
    ("defaults", "model"): str,
    ("defaults", "max_restarts"): int,
    ("defaults", "max_depth"): int,
    ("defaults", "harness"): str,
    ("security", "inherit_env"): bool,
}


# ── Exceptions ────────────────────────────────────────────────────────────────

class ConfigError(Exception):
    """Base exception for config-related errors."""
    pass


class ConfigCorrupt(ConfigError):
    """Raised when config.json contains invalid JSON or missing required keys."""
    pass


# ── Home resolution ───────────────────────────────────────────────────────────

def get_sam_home() -> Path:
    """Resolve SAM_HOME from env var or default to ~/.sam.

    Returns an absolute Path. Does NOT create the directory.
    """
    raw = os.environ.get("SAM_HOME", "~/.sam")
    p = Path(raw).expanduser()
    return p.absolute()


# ── Path helpers ──────────────────────────────────────────────────────────────

def config_path(sam_home: Path = None) -> Path:
    if sam_home is None:
        sam_home = get_sam_home()
    return sam_home / CONFIG_FILENAME


def registry_path(sam_home: Path = None) -> Path:
    if sam_home is None:
        sam_home = get_sam_home()
    return sam_home / REGISTRY_FILENAME


def registry_lock_path(sam_home: Path = None) -> Path:
    if sam_home is None:
        sam_home = get_sam_home()
    return sam_home / LOCK_FILENAME


def locks_dir(sam_home: Path = None) -> Path:
    if sam_home is None:
        sam_home = get_sam_home()
    return sam_home / LOCKS_DIRNAME


def wrapper_path(sam_home: Path = None, harness: str = "pi") -> Path:
    if sam_home is None:
        sam_home = get_sam_home()
    if harness not in HARNESS_WRAPPERS:
        raise ValueError(
            f"unknown harness {harness!r}, expected one of {sorted(HARNESS_WRAPPERS)}"
        )
    return sam_home / BIN_DIRNAME / HARNESS_WRAPPERS[harness]


def resolve_harness(args_harness=None, config: dict = None) -> str:
    """Resolve harness with precedence: CLI flag → $SAM_HARNESS → config → "pi".

    Raises ValueError on unknown harness.
    """
    raw = args_harness or os.environ.get("SAM_HARNESS")
    if not raw and config:
        raw = config.get("defaults", {}).get("harness")
    harness = raw or "pi"
    if harness not in HARNESS_WRAPPERS:
        raise ValueError(
            f"unknown harness {harness!r}, expected one of {sorted(HARNESS_WRAPPERS)}"
        )
    return harness


def bin_dir(sam_home: Path = None) -> Path:
    if sam_home is None:
        sam_home = get_sam_home()
    return sam_home / BIN_DIRNAME


def agents_dir(sam_home: Path = None) -> Path:
    if sam_home is None:
        sam_home = get_sam_home()
    return sam_home / AGENTS_DIRNAME


def tasks_dir(sam_home: Path = None) -> Path:
    if sam_home is None:
        sam_home = get_sam_home()
    return sam_home / TASKS_DIRNAME


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(sam_home: Path = None) -> dict:
    """Load and validate ~/.sam/config.json.

    If the file does not exist, returns DEFAULT_CONFIG.
    If the file exists but is unparseable, raises ConfigCorrupt.
    Missing keys are filled from DEFAULT_CONFIG.
    Unknown/extra keys are ignored (forward-compatible).

    Returns a dict with the merged configuration.
    """
    if sam_home is None:
        sam_home = get_sam_home()

    cfg_path = config_path(sam_home)

    if not cfg_path.exists():
        return dict(DEFAULT_CONFIG)

    try:
        raw_text = cfg_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"Cannot read config file: {e}") from e

    try:
        user_config = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ConfigCorrupt(f"Config file contains invalid JSON at {cfg_path}: {e}") from e

    if not isinstance(user_config, dict):
        raise ConfigCorrupt(f"Config file must contain a JSON object, got {type(user_config).__name__}")

    # Missing top-level sections are corrupt (leaf keys are backfilled from defaults)
    for section in ("defaults", "security"):
        if section not in user_config or not isinstance(user_config[section], dict):
            raise ConfigCorrupt(
                f"Config at {cfg_path} is missing required key: {section}"
            )

    # Merge with defaults: user values override, missing keys filled from DEFAULT_CONFIG
    merged = _deep_merge(dict(DEFAULT_CONFIG), user_config)

    # Validate required keys and types
    _validate_config(merged, cfg_path)

    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Returns new dict."""
    result = {}
    all_keys = set(base.keys()) | set(override.keys())
    for k in all_keys:
        if k in base and k in override:
            if isinstance(base[k], dict) and isinstance(override[k], dict):
                result[k] = _deep_merge(base[k], override[k])
            else:
                result[k] = override[k]
        elif k in override:
            result[k] = override[k]
        else:
            result[k] = base[k]
    return result


def _validate_config(cfg: dict, cfg_path: Path) -> None:
    """Validate that required keys exist and have the correct types.

    Raises ConfigCorrupt on mismatch.
    """
    for keys, expected_type in REQUIRED_CONFIG_KEYS.items():
        value = cfg
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                raise ConfigCorrupt(
                    f"Config at {cfg_path} is missing required key: {' -> '.join(keys)}"
                )
            value = value[key]
        if not isinstance(value, expected_type):
            raise ConfigCorrupt(
                f"Config key {' -> '.join(keys)} should be {expected_type.__name__}, "
                f"got {type(value).__name__} ({value!r})"
            )


# ── Directory initialization ──────────────────────────────────────────────────

def save_config(data: dict, sam_home: Path = None) -> None:
    """Atomically write config.json with mode 0o600. Thin wrapper over _write_file_atomic."""
    if sam_home is None:
        sam_home = get_sam_home()
    path = config_path(sam_home)
    _write_file_atomic(path=path, data=json.dumps(data, indent=2) + "\n", mode=0o600)


def init_sam_home(sam_home: Path = None, force: bool = False) -> None:
    """Create the ~/.sam/ directory tree idempotently.

    Creates: sam_home, bin/, agents/, tasks/, locks/
    If force or config.json doesn't exist, writes default config.
    Does NOT create registry.json or touch existing registry data.

    Raises PermissionError or ConfigError on failure.
    """
    if sam_home is None:
        sam_home = get_sam_home()

    _ensure_dir(sam_home, 0o700)
    _ensure_dir(bin_dir(sam_home), 0o700)
    _ensure_dir(agents_dir(sam_home), 0o700)
    _ensure_dir(tasks_dir(sam_home), 0o700)
    _ensure_dir(locks_dir(sam_home), 0o700)

    cfg_path = config_path(sam_home)
    if force or not cfg_path.exists():
        _write_file_atomic(
            path=cfg_path,
            data=json.dumps(DEFAULT_CONFIG, indent=2) + "\n",
            mode=0o600,
        )


def _ensure_dir(path: Path, mode: int) -> None:
    """Create directory with mode if it doesn't exist. No chmod after creation."""
    try:
        path.mkdir(mode=mode, parents=False, exist_ok=True)
    except FileExistsError:
        # path exists but is not a directory
        raise ConfigError(f"Cannot create directory {path}: a file with that name already exists")
    except PermissionError:
        raise
    except OSError as e:
        raise ConfigError(f"Cannot create directory {path}: {e}") from e


def _write_file_atomic(path: Path, data: str, mode: int) -> None:
    """Write data to path atomically using temp file + os.replace.

    Creates file with the specified mode using os.open.
    Never uses os.chmod after creation.
    """
    directory = path.parent
    # Write to a temp file in the same directory (ensures atomic rename)
    import tempfile  # local import to keep top-level imports minimal

    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(directory),
            prefix=".tmp-",
            suffix=".json",
        )
        # Set permissions via os.open flags were set by mkstemp (0o600 by default on most systems)
        # But mkstemp respects umask. We want explicit 0o600.
        # Close and reopen with explicit mode to be certain.
        os.close(fd)
        os.remove(tmp_path)

        fd = os.open(
            tmp_path,
            os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW,
            mode,
        )
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = None

        os.replace(tmp_path, str(path))

        # fsync the parent directory to ensure metadata is on disk
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    except Exception:
        # Clean up temp file on failure
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
