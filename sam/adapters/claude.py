"""Claude Code adapter: ``claude -p --output-format stream-json --verbose``, prompt on stdin.

Verified on claude 2.1.287 (Windows): events system/init (carries session_id),
assistant, result. The result event has is_error, subtype, result (text),
usage, total_cost_usd. Headless runs cannot answer permission prompts, so
--dangerously-skip-permissions is always passed (same policy as opencode --auto).
With no network claude never exits: the runner's watchdog handles that.
"""

from sam.adapters import Adapter, json_objects, valid_id


class ClaudeAdapter(Adapter):
    name = "claude"
    tag = "CC"
    executable = "claude"
    session_kind = "pointer"
    prompt_via = "stdin"
    reasoning = "effort"

    def build_command(self, prefix, model, task_path, prompt_text,
                      session_path, session_id, reasoning, cwd):
        cmd = list(prefix) + ["-p", "--output-format", "stream-json", "--verbose",
                              "--model", model, "--dangerously-skip-permissions"]
        if reasoning:
            cmd += ["--effort", reasoning]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    def sniff_session_id(self, obj):
        v = obj.get("session_id")
        return v if valid_id(v) else None

    def is_final_event(self, obj):
        return obj.get("type") == "result"

    def parse(self, text, exit_code):
        session, final = None, None
        for obj in json_objects(text):
            session = self.sniff_session_id(obj) or session
            if obj.get("type") == "result":
                final = obj
        if final is None:
            tail = text.strip()[-400:]
            return {"result": None, "session_id": session, "usage": None,
                    "errors": [tail or "claude printed no result event"]}
        res = final.get("result") if isinstance(final.get("result"), str) else None
        errors = []
        if final.get("is_error") or final.get("subtype") not in (None, "success"):
            errors.append((res or "claude result subtype %s" % final.get("subtype"))[:600])
        if not errors and final.get("stop_reason") == "tool_use":
            errors.append("claude exited in the middle of a tool call (stop_reason tool_use); "
                          "the task is not finished - resume the session")
        usage = None
        if isinstance(final.get("usage"), dict):
            u = final["usage"]
            usage = {"input_tokens": u.get("input_tokens"),
                     "output_tokens": u.get("output_tokens"),
                     "cache_read_tokens": u.get("cache_read_input_tokens"),
                     "cache_write_tokens": u.get("cache_creation_input_tokens"),
                     "cost": final.get("total_cost_usd"),
                     "num_turns": final.get("num_turns")}
        return {"result": (res.strip() or None) if res and not errors else (res or None),
                "session_id": session, "usage": usage, "errors": errors}
