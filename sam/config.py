#!/usr/bin/env python3
"""SAM config module: load config, resolve SAM_HOME, initialize directory tree.

Spec source: final-specification-v3.md + reviews-phase-f-batch1.md (GLM-5.2 config.py spec)
"""

import json
import os
import shutil
from pathlib import Path

from sam import plat as sam_plat


# ── Defaults ──────────────────────────────────────────────────────────────────

# pi cannot use OpenCode's free tier (403 FreeTierError outside the opencode CLI);
# on Windows the default is a model that was verified to work there.
PI_DEFAULT_MODEL = ("nvidia/meta/muse-glimmer-30b" if os.name == "nt"
                    else "opencode/muse-spark-1.3-contributor-free")
AGY_DEFAULT_MODEL = "gemini-3.8-flash-low"

DEFAULT_CONFIG = {
    "defaults": {
        "model": PI_DEFAULT_MODEL,
        "agy_model": AGY_DEFAULT_MODEL,
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
#: Every harness SAM can launch. pi/agy have legacy wrapper scripts (used on
#: POSIX); everything else, and every harness on Windows, runs through sam/runner.py.
HARNESSES = ("pi", "agy", "opencode", "claude", "codex")
#: Default models of the runner-only harnesses (config key defaults.<harness>_model).
EXTRA_DEFAULT_MODELS = {
    "opencode": "opencode/muse-spark-1.3-contributor-free",
    "claude": "sonnet",
    "codex": "default",
}


def runner_path() -> Path:
    """Absolute path of the generic runner script (sam/runner.py)."""
    return Path(__file__).resolve().parent / "runner.py"


def uses_runner(harness: str) -> bool:
    """True when ``harness`` is launched through sam/runner.py instead of a
    legacy wrapper: always on Windows, always for harnesses without a legacy
    wrapper, and for pi/agy on POSIX only when $SAM_RUNNER == "generic"."""
    if os.name == "nt" or harness not in HARNESS_WRAPPERS:
        return True
    return os.environ.get("SAM_RUNNER") == "generic"


def launcher_path(harness: str) -> Path:
    """What spawn/resume/restart start detached: the generic runner, or the
    legacy wrapper installed by `sam init`. (wrapper_path stays the legacy bin
    path for pi/agy: `sam init` copies the wrapper scripts to that path.)"""
    if uses_runner(harness):
        return runner_path()
    return wrapper_path(harness=harness)


def launcher_allowed(path) -> bool:
    """Allow-list for the executable SAM starts detached."""
    resolved = Path(path).resolve()
    return (resolved.name in set(HARNESS_WRAPPERS.values())
            or resolved == runner_path())
AGENTS_DIRNAME = "agents"
TASKS_DIRNAME = "tasks"
EVENTS_FILENAME = "events.log"
EPOCH_FILENAME = ".reconcile_epoch"

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
    if harness not in HARNESSES:
        raise ValueError(
            f"unknown harness {harness!r}, expected one of {sorted(HARNESSES)}"
        )
    if harness not in HARNESS_WRAPPERS:
        return runner_path()  # no legacy wrapper exists
    return sam_home / BIN_DIRNAME / HARNESS_WRAPPERS[harness]


def resolve_harness(args_harness=None, config: dict = None) -> str:
    """Resolve harness with precedence: CLI flag → $SAM_HARNESS → config → "pi".

    Raises ValueError on unknown harness.
    """
    raw = args_harness or os.environ.get("SAM_HARNESS")
    if not raw and config:
        raw = config.get("defaults", {}).get("harness")
    harness = raw or "pi"
    if harness not in HARNESSES:
        raise ValueError(
            f"unknown harness {harness!r}, expected one of {sorted(HARNESSES)}"
        )
    return harness


def resolve_model(args_model=None, harness: str = "pi", config: dict = None) -> str:
    """Resolve model with precedence: CLI flag → $SAM_MODEL → per-harness default.

    Per-harness default comes from config ``defaults``: ``agy_model`` when
    harness is ``agy``, else ``model`` (pi default, backward-compatible).
    Missing keys fall back to the hardcoded harness defaults.
    """
    if args_model:
        return args_model
    env_model = os.environ.get("SAM_MODEL")
    env_model_harness = os.environ.get("SAM_MODEL_HARNESS")
    # $SAM_MODEL names the model of the harness that set it; a child that
    # spawns a DIFFERENT harness must not inherit it.
    if env_model and (not env_model_harness or env_model_harness == harness):
        return env_model
    defaults = config.get("defaults", {}) if config else {}
    if harness == "agy":
        return defaults.get("agy_model") or defaults.get("model") or AGY_DEFAULT_MODEL
    if harness in EXTRA_DEFAULT_MODELS:
        return defaults.get(harness + "_model") or EXTRA_DEFAULT_MODELS[harness]
    return defaults.get("model") or PI_DEFAULT_MODEL


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


def reconcile_epoch_path(sam_home: Path = None) -> Path:
    if sam_home is None:
        sam_home = get_sam_home()
    return sam_home / EPOCH_FILENAME


def get_reconcile_epoch(sam_home: Path = None):
    """Return UTC datetime of reconcile epoch marker if present, else None."""
    path = reconcile_epoch_path(sam_home)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
        from datetime import datetime, timezone
        from sam import run_times as sam_run_times
        dt = sam_run_times.parse_timestamp(content)
        if dt is not None:
            return dt
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime, timezone.utc)
    except Exception:
        return None


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
    # Optional per-harness override: type-check only when present.
    agy_model = cfg.get("defaults", {}).get("agy_model")
    if agy_model is not None and not isinstance(agy_model, str):
        raise ConfigCorrupt(
            f"Config key defaults -> agy_model should be str, "
            f"got {type(agy_model).__name__} ({agy_model!r})"
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

    epoch_p = reconcile_epoch_path(sam_home)
    if force or not epoch_p.exists():
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_file_atomic(path=epoch_p, data=now_iso + "\n", mode=0o600)


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
            os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = None

        sam_plat.replace(tmp_path, str(path))

        # fsync the parent directory to ensure metadata is on disk (POSIX only)
        sam_plat.fsync_dir(directory)

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
