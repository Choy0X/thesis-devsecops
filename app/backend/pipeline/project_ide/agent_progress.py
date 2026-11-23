"""Project the local checkpoint into small, evidence-backed recovery context."""
from __future__ import annotations

import json


EFFECT_TOOLS = {"edit_file", "create_file", "run_tests"}


def tool_observation(part):
    state = part["state"]
    arguments = state.get("input") or {}
    if not isinstance(arguments, dict):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            arguments = {}
    output = state.get("output")
    output = output if isinstance(output, dict) else {}
    path = output.get("path") or (arguments.get("path") if isinstance(arguments, dict) else None)
    result = {"tool": part["tool"], "status": state["status"]}
    if isinstance(path, str):
        result["path"] = path[:260]
    if part["tool"] == "read_file" and isinstance(output.get("start"), int) and isinstance(output.get("end"), int):
        result.update(start=output["start"], end=output["end"])
    if isinstance(output.get("exit_code"), int):
        result["exit_code"] = output["exit_code"]
    elif part["tool"] == "run_tests" and output.get("status"):
        result["outcome"] = output["status"]
    if output.get("version"):
        result["version"] = output["version"]
    return result


def observed_tools(parts):
    return [tool_observation(part) for part in parts if part.get("type") == "tool"]


def unsettled_effects(data):
    return [item for item in data.get("effects", {}).values() if item["status"] == "running"]


def plan_for_checkpoint(store, chat_id):
    with store.connect() as connection:
        row = connection.execute("""SELECT payload FROM events WHERE chat_id=? AND type='tool.execution'
            AND json_extract(payload,'$.runtime')='pydantic_ai' AND json_extract(payload,'$.tool')='todowrite'
            AND json_extract(payload,'$.state.status')='completed' ORDER BY sequence DESC LIMIT 1""", (chat_id,)).fetchone()
    if row:
        return json.loads(row["payload"])["state"]["output"]["todos"]
    return []


def guidance(plan, data):
    rows = []
    if plan:
        current = next((step for step in plan if step["status"] == "in_progress"), None)
        outstanding = [step for step in plan if step["status"] == "pending"]
        steps = ([current] if current else []) + outstanding + [step for step in plan if step["status"] in ("completed", "cancelled")]
        rows.append("Saved checklist (agent estimates, latest user request takes precedence): " +
                    " | ".join(f"{step['status']}: {step['content']}" for step in steps))
    for decision in data.get("decisions", [])[-3:]:
        for question, answer in zip(decision["request"]["questions"], decision["answers"]):
            rows.insert(0, "User decision: " + json.dumps({"question": question["question"], "answer": answer[0]}, ensure_ascii=False))
    inspected = {}
    for observation in data.get("observations", []):
        if observation["tool"] == "read_file" and observation["status"] == "completed" and "end" in observation:
            ranges = inspected.setdefault(observation["path"], [])
            span = f"{observation['start']}-{observation['end']}"
            if span not in ranges:
                ranges.append(span)
    if inspected:
        rows.append("Already read: " + "; ".join(f"{path} ({', '.join(spans[-3:])})" for path, spans in list(inspected.items())[-6:]) +
                    ". Use targeted search for missing evidence before rereading these ranges.")
    if data.get("attempts"):
        observations = [item for item in data.get("observations", []) if item["tool"] not in EFFECT_TOOLS and item["status"] != "running"]
        effects = sorted((item for item in data.get("effects", {}).values() if item["status"] in {"completed", "denied"}),
                         key=lambda item: item.get("recorded_at", ""))
        # Preserve completed writes and tests before lower-impact reads within the token budget.
        recent = effects[-5:] + observations[-4:]
        rows[:0] = ["Continue the interrupted request. Reuse observed results. Verify any uncertain action before doing it again. Approvals are fresh."]
        if recent:
            rows.append("Recorded results: " + json.dumps(recent, ensure_ascii=False, separators=(",", ":")))
    if data.get("attempts") and (uncertain := unsettled_effects(data)):
        rows.insert(0, "Unverified operations: " + json.dumps(uncertain, ensure_ascii=False))
    return "\n".join(rows)
