# DevSecOps thesis

This repository accompanies *AI-Driven Autonomous Vulnerability Detection, Proactive CVE Prediction, and Self-Patching Framework for Software Dependencies and Codebases*, a Computer Science BSc thesis by Zoubairy Marouane at the University of Debrecen.

[Read the thesis draft](thesis/thesis.pdf). This is the 40-page revision prepared after the November 2026 supervisor review. The thesis describes the framework, the data mining and training procedures, the evaluation, and the use of AI tools.

## Run the desktop application

The desktop application combines a Pyloid window, a React interface, a FastAPI backend, and a local SQLite database. From the Data and Campaigns pages you can collect public advisories, mine fix commits, and create and monitor collection campaigns. In Projects, open a local repository, select a model, and start a coding conversation. The agent can read project files, propose edits, and run project tests. Review changes before applying them to your checkout.

Use Python 3.11 and Node.js 18 or newer with npm. In an activated Python virtual environment, run these commands from the repository root.

~~~sh
python -m pip install -r requirements.txt
npm --prefix app/frontend ci
npm --prefix app/frontend run build
python -m app.desktop
~~~

For a server-only session, set the environment variable `ZTSA_HEADLESS=1` before launching. The launcher prints a local URL. Start Ollama and select an installed compatible model to use Projects chat. Mining public sources requires network access. Project checks that run in containers require Docker. Execution-based vulnerability reproduction is unavailable in this distribution. Static code review, dependency analysis, and project regression tests are included.

## Implementation

- [Campaign management](app/backend/campaigns_api.py) handles collection plans, job controls, resource checks, and progress.
- [Data mining](app/backend/pipeline/ingest) follows advisories to upstream commits and extracts vulnerable and fixed code pairs.
- [Projects](app/backend/pipeline/project_ide) provides model-backed coding sessions, workspaces, patch review, and terminal integration.
- [Training](training/finetune) contains the sampling, paired objectives, model training, and evaluation code.

To run the backend tests, install `requirements-dev.txt` in the same environment and run `python -m pytest -q tests`. For the interface tests, run `npm --prefix app/frontend test`.

## Experiment records

The [experiment directories](experiments) contain training configurations, evaluation plans, score reports, and per-view decisions for selected runs. The final adapter's [scored views](experiments/grounded_eval_v40a/scored_views.json) and [second-stage scores](experiments/grounded_eval_v40b/scored_views.json) each cover 391 evaluation views. The [matched-view scores](experiments/local_matched/scored_views.json) cover the 86 inputs used to compare the local base model and adapter. Run `python experiments/local_matched/score_pairs.py` to reproduce the local matched-pair totals. A view is one evaluation input. Several views can represent the same underlying code unit.

The [training record](experiments/training-record.md) summarizes the experimental runs and recipes. From the repository root, `python experiments/full_pair_scores.py` reproduces the full-pair and detection scores. With NumPy and SciPy installed, `python experiments/paired_uncertainty.py` reproduces the grouped intervals and paired checkpoint test.

The [dated acceptance rule](notes/grounded_v40/PRE_DECLARED_RULE.md) records the criteria set before training. The count files beside it report mining yields by weakness class. Local dataset paths are omitted from the configuration copies. Raw mined code, blind-partition inputs, trained weights, and execution logs are held separately.
