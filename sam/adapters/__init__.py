"""sam.adapters - one small class per harness CLI, used by sam/runner.py.

An adapter knows three things about its CLI: how to build the command line,
how the prompt gets in, and how to read the output stream. It never starts
processes and never touches the registry.
"""

import json
import re

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def valid_id(value):
    return isinstance(value, str) and bool(_ID_RE.match(value))


def json_objects(text):
    """Yield every JSON object found one-per-line in ``text`` (others skipped)."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


class Adapter:
    """Base class. Subclasses override the attributes and three methods."""

    name = "base"
    tag = "XX"                 # sentinel tag: ##<tag>_BEGIN_<hex> in output.log
    executable = "base"        # name looked up on PATH
    session_kind = "pointer"   # "pointer": session file holds an id; "file": the CLI owns the file
    prompt_via = "stdin"       # "stdin" | "argv" | "taskfile"
    max_argv_chars = 30000     # Windows command line limit is 32767
    reasoning = "effort"       # which SAM flag this harness accepts: "effort" | "thinking"
    first_output_timeout_s = 300   # no output at all for this long => watchdog kill
    idle_timeout_s = 3600          # no NEW output and no CPU use for this long => watchdog kill
    exit_grace_s = 60              # still alive this long after its final event => killed, run counted as finished

    def build_command(self, prefix, model, task_path, prompt_text,
                      session_path, session_id, reasoning, cwd):
        """Return the full argv (list of str). ``prefix`` is the resolved executable."""
        raise NotImplementedError

    def sniff_session_id(self, obj):
        """Return the session id carried by one output event, or None."""
        return None

    def is_final_event(self, obj):
        """True for the event after which the CLI should exit by itself.
        The runner then allows exit_grace_s seconds and kills a CLI that
        lingers (known hang-at-exit bugs) without failing the run."""
        return False

    def parse(self, text, exit_code):
        """Read the whole captured output. Returns a dict with keys:
        result (str|None), session_id (str|None), usage (dict|None),
        errors (list of str; empty when the run succeeded)."""
        raise NotImplementedError


def _load():
    from sam.adapters.agy import AgyAdapter
    from sam.adapters.claude import ClaudeAdapter
    from sam.adapters.codex import CodexAdapter
    from sam.adapters.opencode import OpencodeAdapter
    from sam.adapters.pi import PiAdapter
    return {a.name: a for a in (PiAdapter(), AgyAdapter(), OpencodeAdapter(),
                                ClaudeAdapter(), CodexAdapter())}


_ADAPTERS = None


def get_adapter(name):
    global _ADAPTERS
    if _ADAPTERS is None:
        _ADAPTERS = _load()
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise ValueError("unknown harness %r, expected one of %s"
                         % (name, sorted(_ADAPTERS)))


def adapter_names():
    global _ADAPTERS
    if _ADAPTERS is None:
        _ADAPTERS = _load()
    return sorted(_ADAPTERS)
