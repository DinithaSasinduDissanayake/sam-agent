"""Codex adapter: ``codex exec --json ... -`` with the prompt on stdin.

Partly verified on codex-cli 0.160.0 (Windows): thread.started{thread_id},
turn.started, error{message}, turn.failed{error.message} were captured for real
(auth failure). The success events (item.completed with item.type
"agent_message", turn.completed{usage}) follow the Codex documentation and are
NOT yet verified on this machine. Transient "Reconnecting..." error events are
not failures by themselves; only turn.failed or a missing turn.completed is.
"""

from sam.adapters import Adapter, json_objects, valid_id


class CodexAdapter(Adapter):
    name = "codex"
    tag = "CX"
    executable = "codex"
    session_kind = "pointer"
    prompt_via = "stdin"
    reasoning = "effort"

    def build_command(self, prefix, model, task_path, prompt_text,
                      session_path, session_id, reasoning, cwd):
        cmd = list(prefix) + ["exec"]
        if session_id:
            cmd += ["resume"]
        cmd += ["--json", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox"]
        if model and model != "default":
            cmd += ["-m", model]
        if reasoning:
            cmd += ["-c", "model_reasoning_effort=%s" % reasoning]
        if session_id:
            cmd += [session_id]
        cmd += ["-"]
        return cmd

    def sniff_session_id(self, obj):
        if obj.get("type") == "thread.started" and valid_id(obj.get("thread_id")):
            return obj["thread_id"]
        return None

    def is_final_event(self, obj):
        return obj.get("type") in ("turn.completed", "turn.failed")

    def parse(self, text, exit_code):
        session, result, usage, errors = None, None, None, []
        completed, last_error = False, None
        for obj in json_objects(text):
            session = self.sniff_session_id(obj) or session
            t = obj.get("type")
            if t == "item.completed" and isinstance(obj.get("item"), dict):
                item = obj["item"]
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    result = item["text"]
            elif t == "turn.completed":
                completed = True
                if isinstance(obj.get("usage"), dict):
                    u = obj["usage"]
                    usage = {"input_tokens": u.get("input_tokens"),
                             "output_tokens": u.get("output_tokens"),
                             "cache_read_tokens": u.get("cached_input_tokens")}
            elif t == "turn.failed":
                err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
                errors.append(str(err.get("message") or "turn failed")[:600])
            elif t == "error" and isinstance(obj.get("message"), str):
                last_error = obj["message"]
        if not completed and not errors:
            errors.append((last_error or text.strip()[-400:]
                           or "codex printed no turn.completed event")[:600])
        return {"result": (result.strip() or None) if result else None,
                "session_id": session, "usage": usage, "errors": errors}
