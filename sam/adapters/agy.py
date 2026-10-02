"""Antigravity (agy) adapter: ``agy --output-format stream-json -p <prompt>``.

Verified on agy 1.2.14 (Windows): agy has NO stdin/file prompt option, so the
prompt is one argv element (hard limit ~32 K characters on Windows). Events:
init / step_update / result; the result event nests the envelope
{conversation_id, status, response, usage, error}. A bad --conversation id is
NOT an error for agy (it silently starts a new conversation, exit 0), so the
runner compares the returned id with the requested one.
"""

from sam.adapters import Adapter, json_objects, valid_id


class AgyAdapter(Adapter):
    name = "agy"
    tag = "AGY"
    executable = "agy"
    session_kind = "pointer"
    prompt_via = "argv"
    reasoning = "effort"

    def build_command(self, prefix, model, task_path, prompt_text,
                      session_path, session_id, reasoning, cwd):
        cmd = list(prefix) + ["--model", model, "--output-format", "stream-json",
                              "--print-timeout", "0s",
                              "--dangerously-skip-permissions"]
        if session_id:
            cmd += ["--conversation", session_id]
        if reasoning:
            cmd += ["--effort", reasoning]
        cmd += ["-p", prompt_text]
        return cmd

    @staticmethod
    def _envelope(obj):
        if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
            return obj["result"]
        if "event" not in obj and "status" in obj:
            return obj
        return None

    def sniff_session_id(self, obj):
        for src in (obj, obj.get("init"), obj.get("step_update"), obj.get("result")):
            if isinstance(src, dict) and valid_id(src.get("conversation_id")):
                return src["conversation_id"]
        return None

    def is_final_event(self, obj):
        return obj.get("event") == "result"

    def parse(self, text, exit_code):
        env, session = None, None
        for obj in json_objects(text):
            session = self.sniff_session_id(obj) or session
            found = self._envelope(obj)
            if found is not None:
                env = found
        if env is None:
            tail = text.strip()[-400:]
            return {"result": None, "session_id": session, "usage": None,
                    "errors": [tail or "agy printed no result envelope"]}
        errors = []
        if env.get("status") != "SUCCESS":
            errors.append(str(env.get("error") or "agy status %s" % env.get("status"))[:600])
        resp = env.get("response") if isinstance(env.get("response"), str) else None
        if valid_id(env.get("conversation_id")):
            session = env["conversation_id"]
        usage = env.get("usage") if isinstance(env.get("usage"), dict) else None
        return {"result": (resp.strip() or None) if resp else None,
                "session_id": session, "usage": usage, "errors": errors}
