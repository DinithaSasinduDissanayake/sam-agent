"""pi adapter: ``pi --print --mode json --model M --session FILE @TASK``.

Verified on pi 0.84.4 (Windows). The task is passed as an @file reference (no
size limit). ``--mode json`` is used because plain ``--print`` cannot tell a
provider error from an answer and ``--mode json`` exits 0 on provider errors:
the adapter reads the last assistant message_end event instead
(stopReason "stop" = answer, stopReason "error" = failure + errorMessage).
The session is a file owned by pi (session_kind "file"); resume = same file.
"""

from sam.adapters import Adapter, json_objects


class PiAdapter(Adapter):
    name = "pi"
    tag = "PI"
    executable = "pi"
    session_kind = "file"
    prompt_via = "taskfile"
    reasoning = "thinking"

    def build_command(self, prefix, model, task_path, prompt_text,
                      session_path, session_id, reasoning, cwd):
        cmd = list(prefix) + ["--print", "--mode", "json", "--model", model,
                              "--session", str(session_path)]
        if reasoning:
            cmd += ["--thinking", reasoning]
        cmd += ["@" + str(task_path)]
        return cmd

    def is_final_event(self, obj):
        return obj.get("type") == "agent_settled"

    def parse(self, text, exit_code):
        result, errors, events = None, [], 0
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                 "cache_write_tokens": 0, "cost": 0.0}
        seen_usage = False
        last = None
        for obj in json_objects(text):
            if not isinstance(obj.get("type"), str):
                continue
            events += 1
            if obj["type"] != "message_end":
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if msg.get("role") != "assistant":
                continue
            last = msg
            u = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            if u:
                seen_usage = True
                for src, dst in (("input", "input_tokens"), ("output", "output_tokens"),
                                 ("cacheRead", "cache_read_tokens"),
                                 ("cacheWrite", "cache_write_tokens")):
                    if isinstance(u.get(src), (int, float)):
                        usage[dst] += u[src]
                cost = u.get("cost") if isinstance(u.get("cost"), dict) else {}
                if isinstance(cost.get("total"), (int, float)):
                    usage["cost"] += cost["total"]
        if last is None:
            tail = text.strip()[-400:]
            errors.append(tail or "pi printed no assistant message")
        else:
            blocks = last.get("content") if isinstance(last.get("content"), list) else []
            txt = "\n".join(b["text"] for b in blocks if isinstance(b, dict)
                            and b.get("type") == "text" and isinstance(b.get("text"), str))
            if last.get("stopReason") == "stop":
                result = txt.strip() or None
            else:
                result = txt.strip() or None
                errors.append(str(last.get("errorMessage")
                                  or "pi stopReason %s" % last.get("stopReason"))[:600])
        return {"result": result, "session_id": None,
                "usage": usage if seen_usage else None, "errors": errors}
