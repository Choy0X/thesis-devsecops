"""Durable training events and a complete, streaming JSON report. Standard library only."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import threading
import time
import uuid

SCHEMA = "training-analytics/1"
EVENT_SCHEMA = "training-events/1"
ARRAYS = {"segments": "segment", "example_catalogue": "example", "micro_batches": "micro_batch",
          "optimizer_steps": "optimizer_step", "evaluations": "evaluation", "checkpoints": "checkpoint",
          "resources": "resource", "lifecycle": "lifecycle", "phases": "phase_completed", "failures": "failure"}


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def iter_events(path):
    path = pathlib.Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("schema") != EVENT_SCHEMA:
                    raise ValueError("unknown event schema")
                yield event
            except (ValueError, TypeError):
                yield {"type": "corrupt_record", "line": line_number,
                       "detail": "Incomplete or invalid event record skipped; original bytes retained"}


class AnalyticsWriter:
    def __init__(self, run_dir, *, identity):
        self.run_dir = pathlib.Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "training_events.jsonl"
        first = next(iter_events(self.path), {})
        self.run_id = first.get("run_id") or uuid.uuid4().hex
        self.segment_id = None
        self.started = time.monotonic()
        self.phase, self.phase_started = None, self.started
        self._operations = threading.RLock()
        self._condition = threading.Condition()
        self._pending = []
        self._buffered_bytes = 0
        self._worker = None
        self._error = None
        self._stopping = False
        self._closed = False
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("rb+") as stream:
                stream.seek(-1, 2)
                if stream.read(1) != b"\n":
                    stream.seek(0, 2)
                    stream.write(b"\n")
        if not first:
            self.emit("identity", identity=identity)

    def start_segment(self, resume_step=0):
        with self._operations:
            self._ensure_open()
            self.segment_id = uuid.uuid4().hex
            self.started = time.monotonic()
            self.emit("segment", resume_step=resume_step)
            return self.segment_id

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("Analytics writer is closed")
        with self._condition:
            if self._error is not None:
                raise self._error

    def enable_async(self, max_queue_bytes=4 * 1024 * 1024):
        """Buffer ordered JSONL writes, blocking producers at the byte budget.

        The budget includes writes in flight. A record larger than the budget
        drains the queue and writes synchronously. Call close() before exiting
        to retain all accepted events; abrupt process exits can lose buffered work.
        """
        if isinstance(max_queue_bytes, bool) or not isinstance(max_queue_bytes, int) or max_queue_bytes <= 0:
            raise ValueError("max_queue_bytes must be a positive integer")
        with self._operations:
            self._ensure_open()
            if self._worker is not None:
                return
            self._max_queue_bytes = max_queue_bytes
            self._worker = threading.Thread(target=self._write_pending, name="training-analytics", daemon=True)
            self._worker.start()

    def _write_pending(self):
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                while True:
                    with self._condition:
                        while not self._pending and not self._stopping:
                            self._condition.wait()
                        if not self._pending:
                            return
                        batch, self._pending = self._pending, []
                    stream.writelines(line for line, size in batch)
                    stream.flush()
                    with self._condition:
                        self._buffered_bytes -= sum(size for line, size in batch)
                        self._condition.notify_all()
        except BaseException as exc:
            self._remember_error(exc)

    def _remember_error(self, error):
        with self._condition:
            if self._error is None:
                self._error = error
            self._stopping = True
            self._condition.notify_all()

    def flush(self):
        """Wait until every previously accepted event is flushed, or raise its I/O error."""
        with self._operations:
            with self._condition:
                while self._buffered_bytes and self._error is None:
                    self._condition.wait()
                if self._error is not None:
                    raise self._error

    def close(self):
        """Drain accepted events and join the writer. Repeated calls are safe."""
        with self._operations:
            self._closed = True
            with self._condition:
                self._stopping = True
                self._condition.notify_all()
            if self._worker is not None:
                self._worker.join()
            self.flush()

    def emit(self, event_type, **data):
        with self._operations:
            self._ensure_open()
            if event_type == "lifecycle":
                now = time.monotonic()
                if self.phase is not None:
                    self.emit("phase_completed", phase=self.phase, seconds=now - self.phase_started,
                              timing_scope="Host wall time including work, device waits and event I/O; compile time not separately measured")
                self.phase = data.get("phase", data.get("state"))
                self.phase_started = now
            record = self._record(event_type, data)
            line = json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n"
            size = len(line.encode("utf-8"))
            if self._worker is not None and size <= self._max_queue_bytes:
                with self._condition:
                    while self._buffered_bytes + size > self._max_queue_bytes and self._error is None:
                        self._condition.wait()
                    if self._error is not None:
                        raise self._error
                    self._pending.append((line, size))
                    self._buffered_bytes += size
                    self._condition.notify_all()
            else:
                self.flush()
                try:
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(line)
                        stream.flush()
                except OSError as exc:
                    if self._worker is not None:
                        self._remember_error(exc)
                    raise
            return record

    def _record(self, event_type, data):
        raw = {"schema": EVENT_SCHEMA, "run_id": self.run_id, "segment_id": self.segment_id,
               "event_id": uuid.uuid4().hex, "type": event_type,
               "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               "elapsed_seconds": time.monotonic() - self.started, **data}
        record = _clean(raw)
        if record != raw:
            record["measurement_warning"] = "Non-finite values replaced with null"
        return record

    def emit_examples(self, examples):
        """Drain earlier events and synchronously stream the static catalogue."""
        with self._operations:
            self._ensure_open()
            self.flush()
            try:
                with self.path.open("a", encoding="utf-8") as stream:
                    for data in examples:
                        stream.write(json.dumps(self._record("example", data), allow_nan=False, separators=(",", ":")) + "\n")
                    stream.flush()
            except OSError as exc:
                if self._worker is not None:
                    self._remember_error(exc)
                raise

    def consolidate(self, state="unknown"):
        with self._operations:
            self.emit("lifecycle", state=state)
            self.flush()
            return self._consolidate(state)

    def _consolidate(self, state):
        identity, metadata, warnings = {}, {}, []
        cutoffs, steps, phase_seconds = {}, {}, {}
        measured_peak = None
        last_state = state
        for event in iter_events(self.path):
            kind, segment = event["type"], event.get("segment_id")
            if kind == "identity":
                identity = event["identity"]
            elif kind == "metadata":
                metadata.update(event.get("data", {}))
            elif kind == "segment":
                for old in cutoffs:
                    cutoffs[old] = min(cutoffs[old], event["resume_step"])
                cutoffs[segment] = math.inf
            elif kind == "optimizer_step":
                steps[(segment, event["step"])] = {k: event.get(k) for k in ("tokens", "sequences", "loss", "step_seconds")}
            elif kind in ("warning", "corrupt_record"):
                warnings.append(event)
            elif kind == "phase_completed":
                phase_seconds[event["phase"]] = phase_seconds.get(event["phase"], 0) + (event.get("seconds") or 0)
            elif kind == "resource" and event.get("peak_allocated_bytes") is not None:
                measured_peak = max(measured_peak or 0, event["peak_allocated_bytes"])
            if event.get("measurement_warning"):
                warnings.append({"event_id": event["event_id"], "detail": event["measurement_warning"]})
        def canonical(event):
            key = (event.get("segment_id"), event.get("step"))
            return key in steps and key[1] <= cutoffs.get(key[0], -1)
        committed = [data for (segment, step), data in steps.items() if step <= cutoffs.get(segment, -1)]
        summary = {"committed_steps": len(committed), "committed_tokens": sum(x.get("tokens") or 0 for x in committed),
                   "committed_sequences": sum(x.get("sequences") or 0 for x in committed),
                   "completed_step_seconds": sum(x.get("step_seconds") or 0 for x in committed),
                   "completeness": "valid_event_prefix" if any(w.get("type") == "corrupt_record" for w in warnings) else "recorded_history",
                   "definitions": {"canonical": "Completed optimizer work retained by subsequent checkpoint resumes",
                                   "tokens": "Supervised target tokens; padding excluded",
                                   "timing": "Seconds; measured phases include device synchronization",
                                   "loss": "Training objective, not downstream accuracy"}}
        durations = sorted(x["step_seconds"] for x in committed if x.get("step_seconds") is not None)
        losses = [x["loss"] for x in committed if x.get("loss") is not None]
        summary.update(phase_seconds=phase_seconds, measured_peak_allocated_bytes=measured_peak,
                       peak_availability="Allocator peak over all recorded attempts" if measured_peak is not None else "Backend does not expose measured allocator peaks",
                       selected_checkpoint=metadata.get("provenance", {}).get("selected"),
                       loss={"first": losses[0] if losses else None, "last": losses[-1] if losses else None,
                             "minimum": min(losses) if losses else None},
                       step_duration_seconds={"count": len(durations), "minimum": min(durations) if durations else None,
                                              "maximum": max(durations) if durations else None,
                                              "mean": sum(durations) / len(durations) if durations else None},
                       supervised_tokens_per_second=summary["committed_tokens"] / sum(durations) if sum(durations) else None)
        document = {"schema": SCHEMA, "run": {"id": self.run_id, "state": last_state},
                    "configuration": metadata.get("configuration", identity.get("configuration", {})),
                    "provenance": metadata.get("provenance", identity), "environment": metadata.get("environment", {}),
                    "dataset": metadata.get("dataset", {}), "summary": summary, "warnings": warnings}
        target = self.run_dir / "training_analytics.json"
        temp = target.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(_clean(document), allow_nan=False)[:-1])
            for name, event_type in ARRAYS.items():
                stream.write("," + json.dumps(name) + ":[")
                separator = ""
                for event in iter_events(self.path):
                    if event["type"] != event_type:
                        continue
                    if event_type in ("micro_batch", "optimizer_step"):
                        event = {**event, "canonical": canonical(event)}
                    stream.write(separator + json.dumps(event, allow_nan=False, separators=(",", ":")))
                    separator = ","
                stream.write("]")
            stream.write("}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        return target


def rebuild_report(run_dir, state="recovered"):
    """Recover flushed history after an uncatchable process exit."""
    return AnalyticsWriter(run_dir, identity={}).consolidate(state)


def read_report_header(path, max_bytes=4 * 1024 * 1024):
    """Read bounded metadata preceding history arrays, without loading a multi-GB report."""
    decoder = json.JSONDecoder(parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
    buffer, position, result = "", 0, {}
    with pathlib.Path(path).open(encoding="utf-8") as stream:
        while len(buffer.encode("utf-8")) <= max_bytes:
            block = stream.read(16384)
            buffer += block
            try:
                if not position:
                    position = buffer.index("{") + 1
                while True:
                    while position < len(buffer) and buffer[position] in " \r\n\t,":
                        position += 1
                    if position < len(buffer) and buffer[position] == "}":
                        return result
                    key, end = decoder.raw_decode(buffer, position)
                    if key in ARRAYS:
                        return result
                    cursor = end
                    while cursor < len(buffer) and buffer[cursor].isspace():
                        cursor += 1
                    if cursor >= len(buffer):
                        raise json.JSONDecodeError("Need colon", buffer, cursor)
                    if buffer[cursor] != ":":
                        raise ValueError("Invalid report metadata separator")
                    cursor += 1
                    while cursor < len(buffer) and buffer[cursor].isspace():
                        cursor += 1
                    value, end = decoder.raw_decode(buffer, cursor)
                    if end == len(buffer) and block:
                        raise json.JSONDecodeError("Need separator after metadata value", buffer, end)
                    result[key], position = value, end
            except (json.JSONDecodeError, IndexError):
                if not block:
                    raise ValueError("Incomplete analytics report header")
    raise ValueError("Analytics metadata exceeds the 4 MiB header limit")
