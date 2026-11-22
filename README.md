# DevSecOps thesis

This repository accompanies *AI-Driven Autonomous Vulnerability Detection, Proactive CVE Prediction, and Self-Patching Framework for Software Dependencies and Codebases*, a Computer Science BSc thesis by Zoubairy Marouane at the University of Debrecen.

[Read the thesis draft](thesis/thesis.pdf). This is the 55-page version reviewed in November 2026. The thesis describes the framework, the data mining and training procedures, the evaluation, and the use of AI tools.

## Application and training code

The React interface is in [app/frontend](app/frontend). It can be compiled separately. Running the full desktop application requires its service backend. The Python components included here can be used on their own.

- [Manifest readers](app/backend/pipeline/scan/manifests.py) extract dependency names and versions from project manifests and lock files. A declared version range and a locked version carry different meanings in a scan.
- [Advisory references](app/backend/pipeline/ingest/fixrefs.py) map OSV and NVD entries to upstream fix commits. This copy contains the metadata parsing functions used in data collection.
- [Paired training losses](training/finetune/pair_losses.py) contains the two PyTorch objectives extracted from the training implementation.
- [Training sampler](training/finetune/sampler.py) schedules tasks and paired training examples. The [sampler tests](tests/test_sampler_pairs.py) cover pair selection and checkpoint recovery.

With Python 3.11, install the test requirements and run the component tests from the repository root.

~~~sh
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
~~~

To compile the interface, run the following from the repository root.

~~~sh
npm --prefix app/frontend ci
npm --prefix app/frontend run build
~~~

## Experiment records

The [experiment directories](experiments) contain training configurations, evaluation plans, score reports, and per-view decisions for selected runs. The final adapter's [scored views](experiments/grounded_eval_v40a/scored_views.json) and [second-stage scores](experiments/grounded_eval_v40b/scored_views.json) each cover 391 evaluation views. The [matched-view scores](experiments/local_matched/scored_views.json) cover the 86 inputs used to compare the local base model and adapter. Run `python experiments/local_matched/score_pairs.py` to reproduce the local matched-pair totals. A view is one evaluation input. Several views can represent the same underlying code unit.

The [dated acceptance rule](notes/grounded_v40/PRE_DECLARED_RULE.md) records the criteria set before training. The count files beside it report mining yields by weakness class. Local dataset paths are omitted from the configuration copies. Raw mined code, blind-partition inputs, trained weights, and execution logs are held separately.
