"""Local notebook display over durable events; no telemetry server or external account."""
from __future__ import annotations

from collections import deque
import html
import json
import math
import pathlib
import subprocess
import time
import uuid

from training.finetune.analytics import AnalyticsWriter, iter_events


class LiveReader:
    def __init__(self, run_dir, max_points=1500):
        self.path = pathlib.Path(run_dir) / "training_events.jsonl"
        self.position = 0
        self.live_micro_batch_progress = False
        self.result = {"steps": deque(maxlen=max_points), "evaluations": deque(maxlen=100), "resources": deque(maxlen=max_points),
                       "phase": "starting", "training_started": False, "micro_batch": None, "metadata": {}, "warnings": deque(maxlen=10),
                       "controllers": deque(maxlen=30), "eta_steps": deque(maxlen=30), "segment_id": None, "policy_epoch": None}

    def update(self):
        if not self.path.exists():
            return self.result
        with self.path.open("rb") as stream:
            stream.seek(self.position)
            while True:
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                self.position = stream.tell()
                try:
                    event = json.loads(line)
                    self._apply(event)
                except (ValueError, KeyError, TypeError):
                    self.result["warnings"].append({"detail": "Incomplete/invalid event skipped"})
        return self.result

    def _apply(self, event):
        result = self.result
        kind = event["type"]
        if kind == "optimizer_step":
            segment, policy = event.get("segment_id"), event.get("policy_epoch")
            if segment != result["segment_id"] or policy != result["policy_epoch"]:
                result["eta_steps"].clear()
                result.update(segment_id=segment, policy_epoch=policy)
            seconds = event.get("step_seconds")
            if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds > 0:
                result["eta_steps"].append(event)
            result["steps"].append(event)
            result["training_started"] = True
            result["micro_batch"] = None
        elif kind == "evaluation":
            result["evaluations"].append(event)
        elif kind == "resource":
            result["resources"].append(event)
        elif kind == "lifecycle":
            result["phase"] = event.get("phase", event.get("state", result["phase"]))
            result.update({k: event[k] for k in ("target_steps", "backend") if k in event})
            if result["phase"] in SETUP_PHASES:
                result["training_started"] = False
                result["micro_batch"] = None
                result.pop("target_steps", None)
                result["eta_steps"].clear()
                self.live_micro_batch_progress = False
            elif result["phase"] == "training":
                result["training_started"] = True
        elif kind in ("micro_batch_started", "micro_batch"):
            if kind == "micro_batch_started":
                self.live_micro_batch_progress = True
                result["micro_batch"] = event
            elif not self.live_micro_batch_progress:
                result["micro_batch"] = event
        elif kind == "controller":
            result["controllers"].append(event)
            if "policy_epoch" in event and event["policy_epoch"] != result["policy_epoch"]:
                result["policy_epoch"] = event["policy_epoch"]
                result["eta_steps"].clear()
        elif kind == "metadata":
            result["metadata"].update(event["data"])
        elif kind in ("warning", "failure", "corrupt_record"):
            result["warnings"].append(event)
        elif kind == "segment":
            for key in ("steps", "resources", "evaluations"):
                result[key] = deque((row for row in result[key] if row.get("step", 0) <= event["resume_step"]),
                                    maxlen=result[key].maxlen)
            result["micro_batch"] = None
            result["eta_steps"].clear()
            result.update(segment_id=event.get("segment_id"), policy_epoch=None)
            self.live_micro_batch_progress = False


def read_live_snapshot(run_dir, max_points=1500):
    return LiveReader(run_dir, max_points).update()


def render_training_report(run_dir, *, save_dir=None, snapshot=None):
    import matplotlib.pyplot as plt
    snapshot = snapshot or read_live_snapshot(run_dir)
    rows, resources = list(snapshot["steps"]), list(snapshot["resources"])
    figure, axes = plt.subplots(5, 2, figsize=(13, 16), constrained_layout=True)
    fields = [("loss", "Training objective"), ("lr", "Learning rate"), ("grad_norm", "Gradient norm before clipping"),
              ("supervised_tokens_per_second", "Supervised tokens / second"), ("step_seconds", "Step duration (seconds)")]
    for axis, (key, title) in zip(axes.flat, fields):
        axis.plot([r["step"] for r in rows], [r.get(key) for r in rows], label=key)
        axis.set(xlabel="Optimizer step", ylabel=title, title=title)
        axis.grid(alpha=.2)
    if any(r.get("input_tokens_per_second") is not None for r in rows):
        axes[1, 1].plot([r["step"] for r in rows], [r.get("input_tokens_per_second") for r in rows], label="input_tokens_per_second")
        axes[1, 1].set(ylabel="Tokens / second", title="Input and supervised throughput")
        axes[1, 1].legend(fontsize="small")
    for event in snapshot["evaluations"]:
        if event.get("val_loss") is not None:
            axes[0, 0].scatter([event["step"]], [event["val_loss"]], color="tab:orange", label="Validation loss")
    for key in ("lm_loss", "decision_loss", "rank_loss", "invariance_loss"):
        if any(r.get(key) is not None for r in rows):
            axes[2, 1].plot([r["step"] for r in rows], [r.get(key) for r in rows], label=key)
    axes[2, 1].set(xlabel="Optimizer step", ylabel="Loss", title="Objective components")
    if axes[2, 1].lines:
        axes[2, 1].legend()
    for key in sorted({key for r in rows for key in r.get("fields", {})}):
        axes[3, 0].plot([r["step"] for r in rows], [r.get("fields", {}).get(key) for r in rows], label=key)
    axes[3, 0].set(xlabel="Optimizer step", ylabel="Cross-entropy", title="Completion fields")
    if axes[3, 0].lines:
        axes[3, 0].legend(fontsize="small")
    for key in ("allocated_bytes", "reserved_bytes", "free_bytes", "total_bytes", "peak_allocated_bytes", "inactive_split_bytes"):
        measured = [r for r in resources if r.get(key) is not None]
        if measured:
            axes[3, 1].plot([r.get("step", 0) for r in measured], [r[key] / 1e9 for r in measured], label=key)
    axes[3, 1].set(xlabel="Optimizer step", ylabel="GB", title="Accelerator memory (when measured)")
    if axes[3, 1].lines:
        axes[3, 1].legend(fontsize="small")
    for key in ("preparation_seconds", "transfer_seconds", "compute_seconds", "logging_seconds", "controller_seconds"):
        if any(r.get(key) is not None for r in rows):
            axes[4, 0].plot([r["step"] for r in rows], [r.get(key) for r in rows], label=key)
    axes[4, 0].set(xlabel="Optimizer step", ylabel="Seconds", title="Completed step timings")
    if axes[4, 0].lines:
        axes[4, 0].legend(fontsize="small")
    utilization = [r for r in resources if isinstance(r.get("gpu_utilization_percent"), (int, float))
                   and math.isfinite(r["gpu_utilization_percent"])]
    if utilization:
        axes[4, 1].plot([r.get("step", 0) for r in utilization], [r["gpu_utilization_percent"] for r in utilization], label="gpu_utilization_percent")
    else:
        axes[4, 1].text(.5, .5, "GPU utilization unavailable", ha="center", va="center", transform=axes[4, 1].transAxes)
    axes[4, 1].set(xlabel="Optimizer step", ylabel="Percent", title="Measured GPU utilization", ylim=(0, 100))
    figure.suptitle("Training analytics: measured values, display history bounded to 1,500 steps")
    if save_dir:
        folder = pathlib.Path(save_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for suffix in ("png", "svg"):
            figure.savefig(folder / f"training_curves.{suffix}", dpi=180)
    return figure


SETUP_PHASES = {"starting", "initializing", "loading_tokenizer", "loading_model", "preparing_dataset",
                "preparing_adapter", "recording_catalogue", "profiling", "tuning_throughput"}


class NotebookDisplay:
    """Keep status and charts in stable output slots; plot only changed measurements."""
    def __init__(self, publish=None, *, log_start=0):
        self.publish = publish or self._publish
        self.log_start = log_start
        self.ids = {key: "training-" + uuid.uuid4().hex for key in ("status", "chart", "details")}
        self.visible = set()
        self.chart_signature = None
        self.detail_signature = None
        self.phase = None
        self.phase_started = time.monotonic()

    def _publish(self, channel, value, update):
        from IPython.display import HTML, display, update_display
        value = HTML(value) if isinstance(value, str) else value
        (update_display if update else display)(value, display_id=self.ids[channel])

    def _send(self, channel, value):
        self.publish(channel, value, channel in self.visible)
        self.visible.add(channel)

    def update(self, run_dir, state=None, snapshot=None):
        snapshot = snapshot or read_live_snapshot(run_dir)
        rows = snapshot["steps"]
        phase = snapshot["phase"]
        if phase != self.phase:
            self.phase, self.phase_started = phase, time.monotonic()
        active = snapshot.get("training_started", False) and phase not in SETUP_PHASES
        latest = rows[-1] if active and rows else {}
        meta = snapshot["metadata"]
        model = meta.get("provenance", {}).get("model", "initializing")
        message = f"{state or phase} | {model}"
        if active:
            target = snapshot.get("target_steps")
            message += f" | step {latest.get('step', 0)}" + (f" / {target}" if target else "")
            micro = snapshot["micro_batch"]
            if micro:
                accumulation = micro.get('micro_batches') or meta.get("configuration", {}).get("training", {}).get("gradient_accumulation_steps")
                message += f" | micro-batch {micro['micro_batch'] + 1}" + (f" / {accumulation}" if accumulation else "")
                if micro.get("loss") is not None:
                    message += f" | loss {micro['loss']}"
            if target:
                recent = list(snapshot.get("eta_steps", ()))
                if len(recent) >= 3:
                    mean = sum(row["step_seconds"] for row in recent) / len(recent)
                    message += f" | remaining ~{max(0, target - latest.get('step', 0)) * mean / 60:.1f} min (last {len(recent)} committed steps, current segment and policy)"
                else:
                    message += f" | ETA warming up ({len(recent)} / 3 completed steps in current segment and policy)"
        else:
            message += f" | phase elapsed {int(time.monotonic() - self.phase_started)} s"
        log_path = pathlib.Path(run_dir) / "training_console.log"
        tail = ""
        if log_path.exists():
            with log_path.open("rb") as stream:
                start = max(self.log_start, log_path.stat().st_size - 12000)
                stream.seek(start)
                if start > self.log_start:
                    stream.readline()  # Discard a partial first line from the bounded tail.
                tail = "\n".join(stream.read().decode("utf-8", errors="replace").splitlines()[-12:])
        controllers = list(snapshot.get("controllers", ()))
        resources = list(snapshot.get("resources", ()))
        performance = {key: latest[key] for key in ("input_tokens_per_second", "supervised_tokens_per_second", "preparation_seconds",
            "transfer_seconds", "compute_seconds", "logging_seconds", "controller_seconds", "policy_epoch", "controller_mode", "retry_count") if key in latest}
        detail = json.dumps({"dataset": meta.get("dataset"), "warnings": list(snapshot["warnings"]),
                            "performance": performance, "resource": resources[-1] if resources else None,
                            "controller_decisions": controllers}, indent=2)
        if resources:
            utilization = resources[-1].get("gpu_utilization_percent")
            message += " | GPU utilization: " + (f"{utilization:g}%" if isinstance(utilization, (int, float)) and math.isfinite(utilization) else "unavailable")
        if controllers:
            decision = controllers[-1]
            message += f" | controller: {decision.get('decision', decision.get('action', 'observing'))}"
            if decision.get("reason"):
                message += f" ({decision['reason']})"
        content = ("<h3>" + html.escape(message) + "</h3>"
                   + ("<p>Charts will appear after the first completed optimizer step.</p>" if active and not rows else "")
                   + "<p>Current attempt log (full history remains in training_console.log):</p>"
                   + '<pre style="max-height:220px;overflow:auto;white-space:pre-wrap">' + html.escape(tail) + "</pre>")
        try:
            self._send("status", content)
        except ImportError:
            print(message + "\n" + tail[-2000:])
            return
        if active and (meta.get("dataset") or snapshot["warnings"] or resources or controllers or performance) and detail != self.detail_signature:
            self._send("details", "<details><summary>Dataset, resources, controller and warnings</summary><pre>"
                       + html.escape(detail) + "</pre></details>")
            self.detail_signature = detail
        if not active or not rows:
            if "chart" in self.visible and self.chart_signature is not None:
                self._send("chart", "")
                self.chart_signature = None
            return
        # These are the inputs used by the report, excluding high-frequency micro-batch/status events.
        signature = json.dumps([list(rows), list(snapshot["evaluations"]), list(snapshot["resources"])], sort_keys=True)
        if signature != self.chart_signature:
            import matplotlib.pyplot as plt
            figure = render_training_report(run_dir, snapshot=snapshot)
            try:
                self._send("chart", figure)
            finally:
                plt.close(figure)
            self.chart_signature = signature


def monitor_training(command, *, cwd, run_dir, refresh_seconds=5, display=True):
    """Run an argv list, monitor flushed events and preserve reports on interruption."""
    folder = pathlib.Path(run_dir).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stop_file = folder / ".stop_training"
    stop_file.unlink(missing_ok=True)
    command = list(command)
    reader = LiveReader(folder)
    reader.update()
    reader.result.update(phase="starting", training_started=False, micro_batch=None)
    reader.result.pop("target_steps", None)
    reader.result["warnings"].clear()
    if "training.finetune.runner" in command and "--stop-file" not in command:
        command.extend(["--stop-file", str(stop_file)])
    with (folder / "training_console.log").open("ab") as log:
        view = NotebookDisplay(log_start=log.tell()) if display else None
        child = subprocess.Popen(command, cwd=str(pathlib.Path(cwd).resolve()), stdout=log, stderr=subprocess.STDOUT)
        last_refresh, last_phase = 0., None
        stopping = False
        while child.poll() is None:
            try:
                now = time.monotonic()
                snapshot = reader.update()
                phase = snapshot["phase"]
                if display and (now - last_refresh >= max(5, refresh_seconds) or phase != last_phase):
                    try:
                        view.update(folder, "stopping after the current optimizer step" if stopping else None, snapshot)
                    except Exception as error:
                        print(f"Live display unavailable: {error}. Training and event recording continue.")
                    last_refresh, last_phase = now, phase
                time.sleep(.5)
            except KeyboardInterrupt:
                if stopping:
                    child.terminate()
                stopping = True
                stop_file.touch()
                print("Stop requested. Waiting for an optimizer boundary and checkpoint; interrupt again to terminate.")
        code = child.wait()
    if code and (folder / "training_events.jsonl").exists():
        AnalyticsWriter(folder, identity={}).consolidate("failed")
    if display:
        try:
            view.update(folder, "failed" if code else "process completed; check the recorded run state", reader.update())
        except Exception as error:
            print(f"Final display unavailable: {error}. See training_analytics.json and training_console.log.")
    if code:
        raise subprocess.CalledProcessError(code, command)
    return code


def _phase_tail(path):
    if not path.exists():
        return None
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        for line in reversed(stream.read().splitlines()):
            try:
                row = json.loads(line)
                if row.get("type") == "lifecycle":
                    return row.get("phase", row.get("state"))
            except ValueError:
                pass
    return None
