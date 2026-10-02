"""OpenCode adapter: ``opencode run --format json --auto`` with the prompt on stdin.

Verified on opencode 1.18.34 (Windows): events step_start / text / tool_use /
step_finish / error, each carrying sessionID. ``--dir`` is always passed
because opencode trusts an inherited $PWD over the real working directory.
"""

import json

from sam.adapters import Adapter, json_objects, valid_id


class OpencodeAdapter(Adapter):
    name = "opencode"
    tag = "OC"
    executable = "opencode"
    session_kind = "pointer"
    prompt_via = "stdin"
    reasoning = "effort"
    first_output_timeout_s = 600   # a 503 overload retries ~150 s before the first event

    def build_command(self, prefix, model, task_path, prompt_text,
                      session_path, session_id, reasoning, cwd):
        cmd = list(prefix) + ["run", "--format", "json", "--auto", "-m", model]
        if reasoning:
            cmd += ["--variant", reasoning]
        if session_id:
            cmd += ["--session", session_id]
        if cwd:
            cmd += ["--dir", str(cwd)]
        return cmd

    def sniff_session_id(self, obj):
        v = obj.get("sessionID")
        return v if valid_id(v) else None

    def is_final_event(self, obj):
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        return obj.get("type") == "step_finish" and part.get("reason") == "stop"

    def parse(self, text, exit_code):
        parts, session, errors = {}, None, []
        order = []
        usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0, "cost": 0.0}
        seen_tokens = False
        events = 0
        for ev in json_objects(text):
            etype = ev.get("type")
            if not isinstance(etype, str):
                continue
            events += 1
            session = self.sniff_session_id(ev) or session
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            if etype in ("step_start", "tool_use"):
                parts, order = {}, []          # keep only the text of the last step
            elif etype == "text" and isinstance(part.get("text"), str):
                pid = part.get("id") or len(order)
                if pid not in parts:
                    order.append(pid)
                parts[pid] = part["text"]      # same part id: last value wins
            elif etype == "step_finish":
                tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else None
                if tok:
                    seen_tokens = True
                    for src, dst in (("input", "input_tokens"), ("output", "output_tokens"),
                                     ("reasoning", "reasoning_tokens")):
                        if isinstance(tok.get(src), (int, float)):
                            usage[dst] += tok[src]
                    cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
                    for src, dst in (("read", "cache_read_tokens"), ("write", "cache_write_tokens")):
                        if isinstance(cache.get(src), (int, float)):
                            usage[dst] += cache[src]
                if isinstance(part.get("cost"), (int, float)):
                    usage["cost"] += part["cost"]
            elif etype == "error":
                err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
                data = err.get("data") if isinstance(err.get("data"), dict) else {}
                msg = data.get("message") or json.dumps(err)[:400]
                status = data.get("statusCode")
                errors.append("%s: %s%s" % (err.get("name") or "error", msg,
                                            " (HTTP %s)" % status if status else ""))
        result = "".join(parts[p] for p in order).strip() or None
        if events == 0:
            tail = text.strip()[-400:]
            errors.append(tail or "opencode printed no JSON event")
        return {"result": result, "session_id": session,
                "usage": usage if seen_tokens else None, "errors": errors}
