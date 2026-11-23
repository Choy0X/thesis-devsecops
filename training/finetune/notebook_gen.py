"""The fine-tuning notebook (Colab or Kaggle): a thin, deterministic wrapper around the runner. No torch.

The notebook is made for one bundle: it embeds the bundle's sha256 and checks the uploaded zip against it before anything is unpacked
(with the standard library only, because the package that would do the checking is inside the bundle). It then installs the pinned
requirements, picks an output folder that survives a runtime restart (Kaggle working directory, Google Drive, else a local folder),
and runs ``python -m training.finetune.runner`` over the unpacked bundle. Each runtime session gets a unique workspace; rerunning cells
in that session continues from its checkpoint. A later session can explicitly resume a matching workspace. It holds
no outputs, no execution counts and no credentials, so an unexecuted notebook cannot be mistaken for a result. The environment of the
run (library versions, GPU, precision) is written into ``run_config.json`` by the runner, not asserted here.
"""
from __future__ import annotations

import json
import pathlib
import re

GENERATOR = "training.finetune.notebook_gen/8"

UNPACK = r'''import hashlib, json, pathlib, re, zipfile, zlib


def bundle_sha256(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_bundle(expected_sha256, bundle_path="", roots=None, expected_bytes=None):
    """Select this notebook's bundle by content, including run names and browser duplicate names."""
    guidance = ("Upload the ZIP from Download bundle for the same run as this notebook. "
                "Keep its downloaded name, or set BUNDLE_PATH to its full path. "
                "Wait for the upload to finish. If Drive still has a partial file, upload the complete ZIP from your computer "
                "through the Colab Files panel to /content, then rerun this cell. "
                "Do not edit BUNDLE_SHA256; download both files again from the same run if needed.")
    if bundle_path:
        candidates = [pathlib.Path(bundle_path).expanduser()]
        if not candidates[0].is_file():
            raise FileNotFoundError(f"Bundle path does not exist: {candidates[0]}. {guidance}")
    else:
        if roots is None:
            roots = [(pathlib.Path("/content"), False), (pathlib.Path.cwd(), False),
                     (pathlib.Path("/content/drive/MyDrive"), True), (pathlib.Path("/kaggle/input"), True)]
        candidates, seen = [], set()
        name_pattern = re.compile(r"(?:bundle|.+-bundle)(?: \(\d+\))?\.zip", re.IGNORECASE)
        for root, recursive in roots:
            root = pathlib.Path(root)
            paths = root.rglob("*.zip") if recursive else root.glob("*.zip")
            for path in sorted(paths):
                if not path.is_file() or not name_pattern.fullmatch(path.name):
                    continue
                identity = path.resolve()
                if identity not in seen:
                    seen.add(identity)
                    candidates.append(path)
    if not candidates:
        raise FileNotFoundError(f"No bundle ZIP found. Expected sha256 {expected_sha256}. {guidance}")
    diagnostics = []
    for path in candidates:
        try:
            size, digest, valid = path.stat().st_size, bundle_sha256(path), zipfile.is_zipfile(path)
            if digest == expected_sha256 and valid and (expected_bytes is None or size == expected_bytes):
                return path
            size_note = f" | expected_bytes={expected_bytes}" if expected_bytes is not None else ""
            if expected_bytes is not None and size < expected_bytes and not valid:
                size_note += f" | incomplete upload: {expected_bytes - size} bytes missing"
            diagnostics.append(f"{path} | bytes={size}{size_note} | sha256={digest} | ZIP={'valid' if valid else 'invalid'}")
        except OSError as error:
            diagnostics.append(f"{path} | unreadable: {error}")
    raise RuntimeError(f"No valid ZIP matches the expected bundle sha256 {expected_sha256}.\n"
                       + "\n".join(diagnostics) + "\n" + guidance)


def unpack_and_verify(bundle_zip, dest, expected_sha256):
    """Check the zip is the bundle this notebook was made for, check every file against its manifest, then write them."""
    bundle_zip, dest = pathlib.Path(bundle_zip), pathlib.Path(dest)
    guidance = "Download and upload the bundle again from the same run. Do not edit BUNDLE_SHA256."
    if not zipfile.is_zipfile(bundle_zip):
        raise RuntimeError(f"{bundle_zip} is not a valid ZIP (it may be incomplete or contain a web page). {guidance}")
    digest = bundle_sha256(bundle_zip)
    if digest != expected_sha256:
        raise RuntimeError(f"{bundle_zip}: bundle sha256 {digest} is not the expected {expected_sha256}. {guidance}")
    try:
        with zipfile.ZipFile(bundle_zip) as z:
            if "bundle_manifest.json" not in z.namelist():
                raise RuntimeError("bundle_manifest.json is missing from the bundle")
            manifest = json.loads(z.read("bundle_manifest.json"))
            listed, present = manifest["files"], set(z.namelist()) - {"bundle_manifest.json"}
            for name in list(listed) + z.namelist():
                parts = pathlib.PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                    raise RuntimeError(f"the bundle holds a path that would leave the destination: {name}")
            problems = [f"{n} is missing from the bundle" for n in sorted(listed) if n not in present]
            problems += [f"{n} changed after the bundle was made" for n in sorted(listed)
                         if n in present and hashlib.sha256(z.read(n)).hexdigest() != listed[n]["sha256"]]
            problems += [f"{n} is in the bundle but not in the manifest" for n in sorted(present - set(listed))]
            if problems:
                raise RuntimeError("; ".join(problems))
            for name in z.namelist():
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
    except (zipfile.BadZipFile, EOFError, zlib.error) as error:
        raise RuntimeError(f"{bundle_zip} is not a valid ZIP: {error}. {guidance}") from error
    return manifest
'''

OUTPUT_FOLDER = '''import json, pathlib, sys, uuid

# Leave empty for a new workspace. To resume after a runtime restart, paste the printed workspace path here.
RESUME_WORKSPACE = ""
# To continue a compatible checkpoint from an older bundle, use this instead.
CONTINUE_FROM_WORKSPACE = ""
_CONTINUATION_STAGE = None
_CONTINUATION_PURPOSE = None
if RESUME_WORKSPACE and CONTINUE_FROM_WORKSPACE:
    raise RuntimeError('Set RESUME_WORKSPACE or CONTINUE_FROM_WORKSPACE, not both.')
WORKSPACE_BUNDLE_SHA256 = "__BUNDLE_SHA256__"
PARENT_RUN_ID = "__PARENT_RUN_ID__"
if pathlib.Path("/kaggle/working").exists():
    OUTPUT_ROOT = pathlib.Path("/kaggle/working/finetune")
elif "google.colab" in sys.modules:
    from google.colab import drive
    drive.mount("/content/drive")
    OUTPUT_ROOT = pathlib.Path("/content/drive/MyDrive/finetune")
else:
    OUTPUT_ROOT = pathlib.Path("finetune_output")
    print("no persistent storage found: a restart of this runtime will lose the run")
OUTPUT_ROOT = OUTPUT_ROOT.resolve()
selection = (str(OUTPUT_ROOT), WORKSPACE_BUNDLE_SHA256, PARENT_RUN_ID, RESUME_WORKSPACE, CONTINUE_FROM_WORKSPACE)
if RESUME_WORKSPACE or CONTINUE_FROM_WORKSPACE:
    OUT = pathlib.Path(RESUME_WORKSPACE or CONTINUE_FROM_WORKSPACE).expanduser().resolve()
elif globals().get("_WORKSPACE_SELECTION") == selection:
    OUT = pathlib.Path(_WORKSPACE_PATH)
else:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        OUT = OUTPUT_ROOT / ("run-" + uuid.uuid4().hex)
        try:
            OUT.mkdir(exist_ok=False)
            break
        except FileExistsError:
            continue
    (OUT / "workspace.json").write_text(json.dumps({"schema": "finetune-workspace/1",
        "id": OUT.name, "bundle_sha256": WORKSPACE_BUNDLE_SHA256, "parent_run_id": PARENT_RUN_ID}, indent=2), encoding="utf-8")
try:
    workspace = json.loads((OUT / "workspace.json").read_text(encoding="utf-8"))
except (OSError, ValueError) as error:
    raise RuntimeError(f"{OUT} has no valid workspace receipt. Use the workspace path printed by the previous notebook, or leave both workspace fields empty for a new run.") from error
if (not isinstance(workspace, dict) or workspace.get("schema") != "finetune-workspace/1" or workspace.get("id") != OUT.name
        or (not CONTINUE_FROM_WORKSPACE and (workspace.get("bundle_sha256") != WORKSPACE_BUNDLE_SHA256
            or (PARENT_RUN_ID and workspace.get("parent_run_id") != PARENT_RUN_ID)))):
    raise RuntimeError(f"{OUT} belongs to a different bundle or workspace. Leave RESUME_WORKSPACE empty for a new run.")
RUN_ID = workspace["id"]
_WORKSPACE_SELECTION, _WORKSPACE_PATH = selection, str(OUT)
print("workspace:", OUT)
print("Keep this path to resume this run after a runtime restart.")
if CONTINUE_FROM_WORKSPACE:
    print('Compatible checkpoint continuation will be checked after verifying the new bundle.')
'''

FIND_BUNDLE = '''BUNDLE_SHA256 = "{sha}"
BUNDLE_BYTES = {zip_bytes!r}  # Size of the complete ZIP on disk, not the total size of its unpacked files.
BUNDLE_PATH = ""  # Optional: full path to the uploaded ZIP. Leave empty to find the matching bundle automatically.
print("expected bundle:", BUNDLE_BYTES, "bytes | SHA256:", BUNDLE_SHA256)
BUNDLE_ZIP = find_bundle(BUNDLE_SHA256, bundle_path=BUNDLE_PATH, expected_bytes=BUNDLE_BYTES)
print("bundle:", BUNDLE_ZIP)
'''

UNPACK_RUN = '''WORK = (pathlib.Path("bundle_unpacked") / (RUN_ID + '-' + BUNDLE_SHA256[:12])).resolve()
manifest = unpack_and_verify(BUNDLE_ZIP, WORK, BUNDLE_SHA256)
print("dataset:", manifest["dataset"]["tag"], manifest["dataset"]["status"], "| source:", manifest["source_revision"].get("commit"))
for warning in manifest["warnings"]:
    print("warning:", warning["code"], "-", warning["detail"])
sys.path.insert(0, str(WORK))
_workspace_resume = None
_resume_helper = WORK / 'training' / 'finetune' / 'workspace_resume.py'
if _resume_helper.exists():
    import importlib.util
    _resume_spec = importlib.util.spec_from_file_location('_finetune_workspace_resume', _resume_helper)
    _workspace_resume = importlib.util.module_from_spec(_resume_spec)
    _resume_spec.loader.exec_module(_workspace_resume)
    _continuation = _workspace_resume.prepare_compatible_resume(
        OUT, WORK, BUNDLE_SHA256, PARENT_RUN_ID,
        allow=bool(globals().get('CONTINUE_FROM_WORKSPACE')),
        stage=globals().get('_CONTINUATION_STAGE'), purpose=globals().get('_CONTINUATION_PURPOSE'))
    if _continuation:
        print('Compatible checkpoint retained at step', _continuation['checkpoint_step'],
              '| optimizer, scheduler, RNG, sampler and history preserved.')
elif globals().get('CONTINUE_FROM_WORKSPACE'):
    raise RuntimeError('This older bundle lacks built-in compatible checkpoint continuation. Use the new run6 bundle.')
'''

RESULT = '''import hashlib, json

RUN = OUT / "run"
config = json.loads((RUN / "run_config.json").read_text()) if (RUN / "run_config.json").exists() else {"state": "failed before run initialization", "smoke_test": None}
print("state:", config["state"], "| steps done:", config.get("steps_done"), "| smoke test:", config["smoke_test"])
from training.finetune.notebook_monitor import render_training_report
import matplotlib.pyplot as plt
figure = render_training_report(RUN, save_dir=RUN / "thesis_figures")
display(figure)
plt.close(figure)
analytics = RUN / "training_analytics.json"
print("complete training history:", analytics)
if analytics.exists():
    print("Analytics is also included in outputs.zip. Download individual files when needed.")
# What this run costs, from the one profiled micro-batch (an estimate, not a measurement of the whole run): use it to plan the compute units.
profile = json.loads((RUN / "profile.json").read_text()) if (RUN / "profile.json").exists() else {}
resolved = json.loads((RUN / "resolved_config.json").read_text())["config"]["training"] if (RUN / "resolved_config.json").exists() else {}
environment = config.get("environment", {})
if profile:
    sequences = resolved.get("effective_sequence_batch_size", 32)
    micro_batches = profile.get('micro_batches_per_optimizer_step', sequences)
    per_step = profile["seconds"] * micro_batches
    print("GPU:", [g["name"] for g in environment.get("gpus", [])], "| bf16:", environment.get("bf16_supported"), "| dtype:", config.get("dtype"))
    peak = profile.get('peak_reserved_bytes')
    print("peak reserved accelerator memory:", f"{peak / 1e9:.1f} GB" if peak is not None else "unavailable", "| tokens/s:", profile['tokens_per_second'])
    print(f"about {per_step:.0f} s per optimizer step ({sequences} sequences) -> about {3600 / per_step:.1f} steps per hour (per_step estimate)")
zipped = RUN / "outputs.zip"
if zipped.exists():
    from training.finetune.notebook_downloads import stage, download
    LOCAL_DOWNLOADS = (pathlib.Path('/content') if pathlib.Path('/content').is_dir() else pathlib.Path.cwd()) / 'finetune_downloads' / OUT.name
    staged = stage(zipped, LOCAL_DOWNLOADS, progress=lambda done, total: print(f"Staging outputs.zip: {done:,} / {total:,} bytes", end='\\r', flush=True))
    print("\\nReady:", staged['path'], "| bytes:", staged['bytes'], "| SHA256:", staged['sha256'])
    print("Use the Files panel to download the staged ZIP. For an explicit Colab transfer, run download_artifact() in a separate cell.")
    def download_artifact(name='outputs.zip'):
        if name not in ('outputs.zip', 'training_analytics.json'):
            raise ValueError('Choose outputs.zip or training_analytics.json')
        artifact = stage(RUN / name, LOCAL_DOWNLOADS)
        download(artifact['path'])
else:
    print("outputs.zip does not exist yet: the run did not finish (run the training cell again to resume)")
'''


def _lines(text: str) -> list[str]:
    parts = text.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _markdown(i: int, text: str) -> dict:
    return {"cell_type": "markdown", "id": f"cell-{i:02d}", "metadata": {}, "source": _lines(text)}


def _code(i: int, text: str) -> dict:
    return {"cell_type": "code", "id": f"cell-{i:02d}", "metadata": {}, "execution_count": None, "outputs": [], "source": _lines(text)}


def _trial_build(*, bundle_sha256, requirements, bundle_zip_bytes, total_budget_minutes, parent_run_id):
    import math
    if not math.isfinite(total_budget_minutes) or total_budget_minutes <= 0:
        raise ValueError('total_budget_minutes must be finite and positive')
    bootstrap = pathlib.Path(__file__).with_name('trial_bootstrap.py').read_text(encoding='utf-8')
    tpu_requirements = pathlib.Path(__file__).resolve().parents[1].joinpath('requirements-finetune-tpu.txt').read_text(encoding='utf-8').splitlines()
    launch = ("# Upload the matching small ZIP before running this cell.\n"
              "# The shared timer starts inside bootstrap before verification and installation.\n"
              "from pathlib import Path\n"
              "BUNDLE_PATH = ''  # Optional full path; otherwise locate the matching upload.\n"
              "OUTPUT_ROOT = '/kaggle/working/ztsa-trials' if Path('/kaggle/working').is_dir() else str(Path.cwd() / 'ztsa-trials')\n"
              f"_BOOTSTRAP_SOURCE = {bootstrap!r}\n"
              "_bootstrap = {'__name__': 'trial_notebook_bootstrap'}\n"
              "exec(compile(_BOOTSTRAP_SOURCE, '<trial_notebook_bootstrap>', 'exec'), _bootstrap)\n"
              f"TRIAL = _bootstrap['run'](BUNDLE_PATH, {bundle_sha256!r}, requirements={requirements!r}, output_root=OUTPUT_ROOT, "
              f"total_budget_minutes={total_budget_minutes!r}, parent_run_id={parent_run_id!r}, "
              f"tpu_requirements={tpu_requirements!r}, allow_experimental_tpu=True)\n"
              "print(TRIAL)\n")
    results = ("import json\n"
               "from pathlib import Path\n"
               "folder = Path(TRIAL['run_dir'])\n"
               "status = json.loads((folder / 'trial_config.json').read_text())\n"
               "print('Trial status:', status['state'], status.get('reason', ''))\n"
               "print('Source-task experiment only. No security-accuracy claim.')\n"
               "comparison = folder / 'trial_comparison.json'\n"
               "if comparison.exists(): print(json.dumps(json.loads(comparison.read_text()), indent=2))\n"
               "archive = folder / 'trial_outputs.zip'\n"
               "print('Import trial outputs in the desktop app:', archive)\n"
               "try:\n"
               "    from IPython.display import FileLink, display\n"
               "    display(FileLink(str(archive)))\n"
               "except ImportError: pass\n")
    return {'nbformat': 4, 'nbformat_minor': 5,
            'metadata': {'generator': GENERATOR, 'mode': 'trial', 'total_budget_minutes': total_budget_minutes,
                         'parent_run_id': parent_run_id, 'bundle_sha256': bundle_sha256, 'bundle_zip_bytes': bundle_zip_bytes,
                         'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'}},
            'cells': [_markdown(0, '# Portable 30-minute source trial\n\nThis notebook has not been executed. '
                         'Use a Colab T4, L4, A100, H100 or G4 GPU, or an experimental v5e-1/v6e-1 TPU runtime. '
                         'CUDA uses NF4; TPU uses a separately declared symmetric INT4/BF16 recipe with isolated dependencies. '
                         'These TPU paths require hardware validation and are not equivalent to NF4. '
                         'Upload the matching small bundle, then run the next cell. '
                         'It automatically detects resources, loads the pinned model once, compares the base, trains a fresh adapter and compares it on the same held-out source tasks. '
                         'The total budget includes setup, downloads, probing, training, evaluation and export. '
                         'Insufficient hardware or setup time may produce partial or blocked results. No smaller model is substituted.'),
                      _code(1, launch), _markdown(2, '## Results\n\nDownload trial_outputs.zip and import it as experimental trial outputs. '
                         'The ZIP retains measured resources, progress, raw generations and loss comparison. '
                         'A partial run is not a complete comparison; source tasks do not establish security detection accuracy.'), _code(3, results)]}


def build(*, bundle_sha256: str, requirements, bundle_zip_bytes: int | None = None, stage: int = 1, steps: int | None = None, eval_max: int | None = None,
          purpose: str = "train", session_minutes: float | None = None, loss_mode: str | None = None, eval_views: int | None = None,
          device: str = "auto", parent_run_id: str | None = None, mode: str = 'full', total_budget_minutes: float = 30) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", str(bundle_sha256)):
        raise ValueError("bundle_sha256 must be a 64-character hex sha256")
    if parent_run_id is not None and not re.fullmatch(r'[0-9a-f]{32}', parent_run_id):
        raise ValueError('parent_run_id must be a UUID hex identity')
    if bundle_zip_bytes is not None and (type(bundle_zip_bytes) is not int or bundle_zip_bytes <= 0):
        raise ValueError("bundle_zip_bytes must be a positive integer or None")
    requirements = [r.strip() for r in requirements if r.strip() and not r.strip().startswith("#")]
    if not requirements:
        raise ValueError("requirements must not be empty")
    if device not in ("auto", "cuda", "tpu"):
        raise ValueError("notebook device must be auto, cuda or tpu")
    if stage not in (1, 2):
        raise ValueError("stage must be 1 or 2")
    if purpose not in ("train", "smoke"):
        raise ValueError("purpose must be train or smoke")
    if session_minutes is not None and session_minutes <= 0:
        raise ValueError("session_minutes must be positive")
    if loss_mode not in (None, "per_example_completion_ce", "present_field_normalized_ce"):
        raise ValueError("loss_mode must be per_example_completion_ce or present_field_normalized_ce")
    if mode not in ('trial', 'full'):
        raise ValueError('mode must be trial or full')
    if mode == 'trial':
        if stage != 1:
            raise ValueError('The source trial uses stage 1 and automatic accelerator detection')
        return _trial_build(bundle_sha256=bundle_sha256, requirements=requirements, bundle_zip_bytes=bundle_zip_bytes,
                            total_budget_minutes=total_budget_minutes, parent_run_id=parent_run_id)
    install = "%pip install -q " + " ".join(f'"{r}"' for r in requirements)
    if device == "tpu":
        # Colab's optional vision/audio binaries are tied to its preinstalled torch.
        # This text-only profile replaces torch and does not use those extensions.
        install = "%pip uninstall -y torchvision torchaudio\n" + install
    runner = '!cd "{WORK.resolve()}" && python -m training.finetune.runner --bundle "{WORK.resolve()}"'
    flags = (f'--stage {stage} --purpose {purpose}'
               + (f" --steps {int(steps)}" if steps else "") + (f" --eval-max {int(eval_max)}" if eval_max else "")
               + (f" --session-minutes {session_minutes:g}" if session_minutes else "")
               + (f" --loss-mode {loss_mode}" if loss_mode else "") + f" --device {device}")
    command = ("import shlex, sys\nfrom training.finetune.notebook_monitor import monitor_training\n"
               "command = [sys.executable, '-u', '-m', 'training.finetune.runner', '--bundle', str(WORK.resolve()),\n"
               "           '--run-dir', str((OUT / 'run').resolve())]\n"
               f"command += shlex.split({flags!r})\n"
               "if globals().get('_workspace_resume') is not None:\n"
               f"    command += _workspace_resume.resume_arguments(OUT, stage={stage}, purpose={purpose!r})\n"
               "monitor_training(command, cwd=WORK.resolve(), run_dir=(OUT / 'run').resolve())")
    if stage == 1 and purpose == "train" and device != "tpu":
        controller = ("# Native SOURCE CUDA controller. Set the comparison budget to 0 to skip it.\n"
                      "RESOURCE_CONTROLLER = 'auto'  # auto, observe, off\n"
                      "CONTROLLER_BENCHMARK_SECONDS = 180  # Total matched comparison budget; cached by the runner.\n"
                      "import json\n"
                      "_resolved_path = WORK / 'resolved_config.json'\n"
                      "_model = json.loads(_resolved_path.read_text(encoding='utf-8')).get('config', {}).get('model', {}) if _resolved_path.exists() else {}\n"
                      "if _model.get('serialization') == 'sft_native_chat_v1':\n"
                      "    if RESOURCE_CONTROLLER not in ('auto', 'observe', 'off'):\n"
                      "        raise ValueError('RESOURCE_CONTROLLER must be auto, observe or off')\n"
                      "    if type(CONTROLLER_BENCHMARK_SECONDS) is not int or not 0 <= CONTROLLER_BENCHMARK_SECONDS <= 180:\n"
                      "        raise ValueError('CONTROLLER_BENCHMARK_SECONDS must be an integer from 0 to 180')\n"
                      "    command += ['--resource-controller', RESOURCE_CONTROLLER, '--controller-benchmark-seconds', str(CONTROLLER_BENCHMARK_SECONDS)]\n")
        command = command.replace("monitor_training(command,", controller + "monitor_training(command,", 1)
    if device == "tpu":
        command = command.replace("monitor_training(command,", "command += ['--tpu-validation-report', str((OUT / 'run' / 'tpu_7b_check.json').resolve())]\nmonitor_training(command,")
        runner += " --device tpu"
    else:
        command = ("import os, sys\n"
                   "# Set before the training child imports PyTorch; preserve an explicitly selected allocator.\n"
                   "if sys.platform.startswith('linux') and not any(key in os.environ for key in\n"
                   "        ('PYTORCH_CUDA_ALLOC_CONF', 'PYTORCH_ALLOC_CONF')):\n"
                   "    os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'\n" + command)
    control = "B4" if stage == 2 else ("B3" if loss_mode == "present_field_normalized_ce" else "B2")
    if purpose == 'train':
        preflight = ("# Automatic dataset preflight and a bounded correctness child; valid matching evidence is reused.\n"
                   "from training.finetune.quality_gate import ensure_diagnostics\n"
                   "def _resources_before_correctness():\n"
                   "    import json\n"
                   "    from training.finetune.resolve import read_resolved\n"
                   "    from training.finetune.accelerator import select_backend\n"
                   "    from training.finetune.runtime_resources import preflight\n"
                   "    from training.finetune.runtime_compatibility import detect_runtime\n"
                   f"    if {device!r} == 'tpu' or ({device!r} == 'auto' and detect_runtime()['kind'] == 'tpu'): return\n"
                   f"    backend = select_backend({device!r})\n"
                   "    report = preflight(WORK.resolve(), (OUT / 'run').resolve(), read_resolved(WORK.resolve()), backend=backend)\n"
                   "    (OUT / 'run').mkdir(parents=True, exist_ok=True)\n"
                   "    (OUT / 'run' / 'resource_report.json').write_text(json.dumps(report, indent=2))\n"
                   "    if report['state'] != 'ready': raise RuntimeError('Insufficient runtime resources: ' + '; '.join(report['reasons']))\n"
                   f"ensure_diagnostics(WORK.resolve(), (OUT / 'run').resolve(), device={device!r}, resource_check=_resources_before_correctness)\n\n")
        command = command.replace('import shlex, sys\n', preflight + 'import shlex, sys\n', 1)
    cap = f" --max-views {int(eval_views)}" if eval_views else ""
    evaluation_inputs = ("import re, shlex\n"
        "# Set both values for an external benchmark. Packaged eval_data is found automatically.\n"
        "EVALUATION_DATA = globals().get('EVALUATION_DATA', '')\n"
        "EVALUATION_MANIFEST_SHA256 = globals().get('EVALUATION_MANIFEST_SHA256', '')\n"
        "if bool(EVALUATION_DATA) != bool(EVALUATION_MANIFEST_SHA256):\n"
        "    raise ValueError('An external evaluation dataset needs its independently recorded manifest SHA256')\n"
        "if EVALUATION_MANIFEST_SHA256 and not re.fullmatch('[a-f0-9]{64}', EVALUATION_MANIFEST_SHA256):\n"
        "    raise ValueError('The evaluation manifest SHA256 must contain 64 lowercase hex characters')\n"
        "EVALUATION_FLAGS = shlex.join(['--evaluation-data', EVALUATION_DATA, '--evaluation-manifest-sha256', EVALUATION_MANIFEST_SHA256]) if EVALUATION_DATA else ''\n")
    base_eval = evaluation_inputs + f'{runner} --run-dir "{{OUT.resolve()}}/eval_b0" --evaluate --baseline B0{cap} {{EVALUATION_FLAGS}}'
    tuned_eval = (f'BASELINE = "{control}"   # B2 and B3: stage 1 (ordinary and field-balanced loss), B4: stage 2, B5: invariance on\n'
                  + evaluation_inputs + f'{runner} --run-dir "{{OUT.resolve()}}/eval_{{BASELINE.lower()}}" '
                  f'--evaluate --baseline {{BASELINE}} --adapter-run "{{OUT.resolve()}}/run"{cap} {{EVALUATION_FLAGS}}')
    cells = [
        _markdown(0, "# Fine-tune Qwen2.5-Coder-7B from a verified bundle\n\n"
                     "This notebook has not been run. Nothing here is a training result until every cell has run and the `outputs.zip` "
                     "it writes has been imported into the app. It checks the bundle against the hash it was made for, installs the "
                     "pinned requirements, and runs the same `training.finetune.runner` the tests run. Each new runtime session gets a unique "
                     "workspace. Rerunning training in that workspace continues from its last checkpoint."),
        _markdown(1, "## 1. Runtime"),
        _code(2, "!nvidia-smi || echo \"no GPU: the 7B fine-tune needs one (select a GPU runtime)\"\n"
                 "import sys\nprint(sys.version)"),
        _markdown(3, "## 2. Pinned requirements\n\nPyTorch comes with the runtime; its version is recorded in `run_config.json`. "
                     "The PyArrow pin is compatible with Colab's cuDF requirement (PyArrow >=19,<24). "
                     "If PyArrow was already imported before changing its installed version, restart the session after installing, then rerun from the top."),
        _code(4, install),
        _markdown(5, "## 3. Run workspace\n\nA random workspace isolates this run's training, evaluations and exports from earlier runs. "
                     "Rerunning this cell in the same session keeps the workspace. After a runtime restart, leave `RESUME_WORKSPACE` empty for a new run, "
                     "or paste a previous workspace path to resume that bundle. To continue an existing run5 checkpoint with this newer bundle, "
                     "interrupt the old training cell once and wait for its checkpoint, leave RESUME_WORKSPACE empty, and paste that workspace path "
                     "into CONTINUE_FROM_WORKSPACE. The verified bundle checks identical model/data/optimizer settings before updating the workspace "
                     "binding. Checkpoint tensors and training progress stay in place. Keep Kaggle outputs if you need them after the session ends."),
        _code(6, OUTPUT_FOLDER.replace("__BUNDLE_SHA256__", bundle_sha256).replace('__PARENT_RUN_ID__', parent_run_id or '')
              .replace('_CONTINUATION_STAGE = None', f'_CONTINUATION_STAGE = {stage}')
              .replace('_CONTINUATION_PURPOSE = None', f'_CONTINUATION_PURPOSE = {purpose!r}')),
        _markdown(7, "## 4. Check and unpack the bundle\n\nDownload the bundle and notebook from the same run. Upload the ZIP using Colab's Files panel, "
                     "or put it in mounted Drive or a Kaggle input dataset. Keep its downloaded name (for example, `run1-bundle.zip`); "
                     "Wait for the upload to finish before running these cells. The complete ZIP must have the expected byte count printed below; "
                     "a partially uploaded Drive file is not usable. You can upload the complete file through the Colab Files panel to `/content` instead. "
                     "`bundle.zip` and browser duplicate names such as `run1-bundle (1).zip` also work. The notebook selects a file whose SHA256 matches "
                     "the expected hash below, then checks every file against the bundle's manifest before unpacking. "
                     "If needed, set `BUNDLE_PATH` below to the full path of your uploaded ZIP. Do not edit `BUNDLE_SHA256` to bypass an error. "
                     "A file with a different hash or an invalid ZIP must be downloaded and uploaded again."),
        _code(8, UNPACK),
        _code(9, FIND_BUNDLE.format(sha=bundle_sha256, zip_bytes=bundle_zip_bytes)),
        _code(10, UNPACK_RUN),
        _markdown(11, "## 5. Train\n\nThe runner writes `run_config.json` (with the environment of this runtime), `metrics.jsonl`, the "
                      "validation evaluations, the adapter, the decision head and the calibration into the output folder. "
                      "Setup shows the current phase, elapsed time and recent log; charts appear after the first completed optimizer step. "
                      "The micro-batch counter shows progress within gradient accumulation before the optimizer step increases. "
                      "Charts update in place only when plotted measurements change. Every micro-batch and optimizer step is retained in `training_events.jsonl` and "
                      "the complete `training_analytics.json`. Interrupt once to request a checkpoint at the next optimizer boundary. "
                      "If training fails, the current JSON remains in the output folder and the failure is shown here. "
                      "New full runs require automatic dataset readiness and matching correctness diagnostics. "
                      "The cell checks dataset readiness before loading the model, automatically runs a bounded CUDA correctness pilot when needed, "
                      "and reuses a matching passing report when cells are rerun. No human approval is required. "
                      "It never uses the untouched test partition. Experimental TPU full runs additionally require a validated TPU correctness pilot. "
                      "Run the final held-out evaluation only after choosing a model: `--evaluate --split test` records the untouched-test scope."),
        _code(12, command),
        _markdown(13, "## 6. Result\n\nImport `outputs.zip` in the app (Fine-tuning page). The run counts as trained only after the "
                      "app has checked its identity against the bundle."),
        _code(14, RESULT),
        _markdown(15, "## 7. Evaluate (optional)\n\nAn all-training source corpus cannot measure generated security accuracy. "
                      "Use an independent frozen, reviewed benchmark with audited overlap and the selection role. "
                      "Set EVALUATION_DATA and EVALUATION_MANIFEST_SHA256 in the next cell, or package eval_data during export. "
                      "The runner checks corpus, labels, tasks, identities and overlap before loading model weights. "
                      "Unavailable evaluation writes evaluation_status.json with the reason and remediation; it does not produce scores. "
                      "Each ready cell answers the same validation selection views greedily and writes `eval_b0_outputs.zip`, `eval_b2_outputs.zip`, or the selected baseline's name: the "
                      "untouched base (B0) and the trained control. Import both on the Fine-tuning page; a control counts as evaluated only "
                      "after the app has checked the attempts against the dataset. Generation takes much longer than a training step per "
                      "view, so choose an explicit compute budget before running. A capped run is partial. Keep final test untouched until selection and calibration are complete."),
        _code(16, base_eval),
        _code(17, tuned_eval),
        _code(18, 'import json\nfor name in ("eval_b0", f"eval_{BASELINE.lower()}"):\n'
                  '    z = OUT / name / f"{name}_outputs.zip"\n'
                  '    status = OUT / name / "evaluation_status.json"\n'
                  '    status_text = status.read_text() if status.exists() else ""\n'
                  '    state = json.loads(status_text).get("state") if status_text else None\n'
                  '    print(name, "->", status_text if state and state != "finished" else z if z.exists() else status_text or "not run")'),
    ]
    if device != "tpu":
        cells[11]["source"] = _lines("".join(cells[11]["source"]) + "\n\n"
            "On Linux GPU runtimes, this cell enables expandable CUDA memory segments unless you already selected an allocator. "
            "This can reduce fragmentation with variable sequence lengths; it does not guarantee the model fits. "
            "If CUDA runs out of memory, select a GPU with more memory to preserve the model, context and adapter settings. "
            "Re-running resumes from the latest saved checkpoint; work since that checkpoint must be repeated.")
        if stage == 1 and purpose == "train":
            cells[11]["source"] = _lines("".join(cells[11]["source"]) + "\n\n"
                "SOURCE stage 1 CUDA training uses the native resource controller in auto mode. It records policy decisions, "
                "memory headroom, timings and retries in the durable events. RESOURCE_CONTROLLER also accepts observe or off. "
                "The optional matched comparison has a total budget of at most 180 seconds and reuses matching cached evidence. "
                "Set CONTROLLER_BENCHMARK_SECONDS to 0 to skip the comparison. These runtime controls preserve the resolved training configuration. "
                "CPU execution keeps its existing behavior. ETA uses the last 30 completed optimizer steps from the current session segment "
                "and policy, and warms up again when either changes. Compatible checkpoint continuation uses CONTINUE_FROM_WORKSPACE above.")
    if device == "tpu":
        cells[2] = _code(2, "import os, sys\nos.environ['PJRT_DEVICE'] = 'TPU'\n"
                           "print('Select TPU v5e-1 or v6e-1 in runtime settings. Experimental 7B INT4, context 4096, rank 64.')\nprint(sys.version)")
        cells[3] = _markdown(3, "## 2. TPU requirements\n\nInstall matching PyTorch/XLA 2.9.0 Linux wheels. "
                               "The setup first removes Colab's optional torchvision and torchaudio packages, which require its "
                               "preinstalled PyTorch version and are unused by this text training pipeline. "
                               "Restart the session after installation if torch was already imported. Run from the top after restart. "
                               "This profile uses Qwen2.5-Coder-7B with a distinct INT4 quantization algorithm, not CUDA NF4. "
                               "Memory fit on a 16 GB v5e-1 remains unvalidated until the real model smoke check passes.")
        cells.insert(11, _code(19, "import subprocess, sys\n"
                                  "from training.finetune.quality_gate import ensure_diagnostics\n"
                                  f"ensure_diagnostics(WORK.resolve(), (OUT / 'run').resolve(), purpose={purpose!r}, device='tpu')\n"
                                  "subprocess.run([sys.executable, '-m', 'training.finetune.tpu_validation',\n"
                                  "                '--output', str(OUT / 'run' / 'tpu_runtime_check.json')], cwd=str(WORK), check=True)\n"
                                  "# A separate process releases TPU memory before the real training run. Synthetic probe, not thesis training data.\n"
                                  "subprocess.run([sys.executable, '-u', '-m', 'training.finetune.tpu_validation',\n"
                                  "                '--bundle', str(WORK), '--output', str(OUT / 'run' / 'tpu_7b_check.json')], cwd=str(WORK), check=True)\n"
                                  "print('Selected-model forward/backward, optimizer, evaluation and checkpoint/resume probe passed.')"))
    cells.extend([
        _markdown(20, '## 8. Export the full fine-tuned model (optional)\n\nThe normal outputs contain the adapter. This separate export reloads the pinned base in BF16 on CPU and merges the adapter into Hugging Face safetensors. It needs enough system RAM and disk for the full model and ZIP. It does not change the training settings or establish security accuracy. Leave disabled unless you want this large export.'),
        _code(21, "EXPORT_FULL_MODEL = False\n"
                  "if EXPORT_FULL_MODEL:\n"
                  "    import subprocess, sys\n"
                  "    FULL_MODEL = pathlib.Path('/content/full_model_export') / OUT.name if pathlib.Path('/content').is_dir() else OUT / 'full_model_export'\n"
                  "    subprocess.run([sys.executable, '-u', '-m', 'training.finetune.full_model_export', '--run-dir', str((OUT / 'run').resolve()), '--out-dir', str(FULL_MODEL / 'huggingface')], cwd=WORK.resolve(), check=True)\n"
                  "    print('Full model ZIP:', FULL_MODEL / 'hf-model.zip')\n"
                  "else:\n    print('Full model export disabled. Set EXPORT_FULL_MODEL = True to merge and export.')")
    ])
    return {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                                         "language_info": {"name": "python"}, "accelerator": "TPU" if device == "tpu" else "GPU", "generator": GENERATOR,
                                         "workspace_policy": 1,
                                         "parent_run_id": parent_run_id,
                                         "training_preflight": "automatic-diagnostics/1",
                                         "bundle_sha256": bundle_sha256, "bundle_zip_bytes": bundle_zip_bytes},
            "nbformat": 4, "nbformat_minor": 5}


def upgrade_workspace(notebook: dict, bundle_sha256: str) -> dict:
    """Upgrade saved wrapper paths only, preserving pinned packages and all runner arguments."""
    import copy
    if notebook.get('metadata', {}).get('bundle_sha256') != bundle_sha256:
        raise ValueError('The notebook belongs to a different bundle')
    if notebook['metadata'].get('workspace_policy') == 1:
        return notebook
    updated = copy.deepcopy(notebook)
    cells = {cell['id']: cell for cell in updated['cells']}
    if not {'cell-06', 'cell-10'} <= cells.keys():
        raise ValueError('This notebook has no supported workspace setup')
    cells['cell-06']['source'] = _lines(OUTPUT_FOLDER.replace('__BUNDLE_SHA256__', bundle_sha256).replace('__PARENT_RUN_ID__', ''))
    cells['cell-10']['source'] = _lines(UNPACK_RUN)
    if 'cell-05' in cells:
        cells['cell-05']['source'] = _lines('## 3. Run workspace\n\nEach new runtime session gets a random workspace. '
            'Rerunning this cell keeps that workspace. To resume after a runtime restart, paste the printed path into RESUME_WORKSPACE. '
            'Leave it empty for a new run. Keep Kaggle outputs if you need them after the session ends.')
    for cell_id in ('cell-14', 'cell-21'):
        if cell_id in cells:
            text = ''.join(cells[cell_id]['source'])
            text = text.replace("/ 'finetune_downloads'", "/ 'finetune_downloads' / OUT.name")
            text = text.replace("pathlib.Path('/content/full_model_export') if", "pathlib.Path('/content/full_model_export') / OUT.name if")
            cells[cell_id]['source'] = _lines(text)
    updated['metadata']['workspace_policy'] = 1
    return updated


def write(path, **kw) -> dict:
    """Write the notebook (the same inputs give the same bytes); returns its path and sha256."""
    import hashlib
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(build(**kw), indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(data)
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
