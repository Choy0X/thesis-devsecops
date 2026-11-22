# Grounded v40 (recipe 12): the rule fixed before any training

Written 2026-11-11, after the Linux campaign and before the v40 build is trained. Nothing below is a model result. An addendum (section 5) fixes the per-language numbers once the build
has counted the held-out views; it is committed before the training run is approved.

## 1. What v40 is

v39 rows copied byte for byte (including the sealed blind rows and the 199-view evaluation cohort) plus units of Packagist, RubyGems, Go, Maven, NuGet, crates.io and the Linux kernel,
proved by flow (python, javascript, typescript only) or by the diff (diff_guard, diff_sink, strict_attribution). Catalogue security_chat_v5 (v4's 37 classes first, then new classes).
Recipe 12 is recipe 11 with 7,800 discovery draws and at most 1,300 updates (Stage A of a staged plan sized on the compute balance; the first narrow build with 6,000 draws is kept aside). The units also come
from the npm and PyPI store beyond v39's training units (about 8,100 candidates; C++ units among them). Final dataset: 2,481 training units (16,732 views), 518 development units (4,111 views), 242 transfer
units (1,803 views), 331 blind units (2,605 views); diff-proved units: diff_guard 1,474, diff_sink 644, strict_attribution 907 (30 percent of the diff-proved units at most), no language above 30 percent.

## 2. The acceptance rule (raw model, the same 86 matched views / 40 functions as v36 and v39, applied to the saved answers, no re-run)

v40 is accepted as "no regression on the old classes" only if all four hold:

1. fixed code cleared at least 33 of the 40 fixed views (v39: 33, v36: 35; the v36 value 35 is reported as the aspiration, it is not required here because v39 was already accepted as the baseline);
2. pair test (vulnerable flagged and its fixed twin cleared) at least 28 of 40 pairs (v39: 28);
3. narrated fixes 0 and hard claim-gate failures at most 6 (v39: 0 and 6);
4. right class on the 40 vulnerable views at least 25 (v39: 25), scored against the OLD catalogue (v4 classes only).

If the rule fails, v39 stays the accepted baseline; v40 is then reported as a negative result and not trained further without a new written plan.

## 3. Cohort identity

The 199 evaluation views of v38/v39 must be identical by view id in the v40 evaluation (`--same-cohort-as`). Their prompts keep the v4 serialization for scoring; the new classes are
scored only on new-class views (section 4). A build whose cohort differs by even one id is not evaluated against v39.

## 4. Reported alongside, not part of the acceptance rule

- Per language (python, javascript, typescript, php, ruby, go, java, csharp, rust, c, cpp): positives flagged with the right class, clean cases answered no_finding, number of views.
- Per proof strength (flow, diff_guard, diff_sink, strict_attribution): the same two measures, never merged into one number.
- New classes (memory safety, concurrency, panic and the others of catalogue v5) apart from the old classes.
- Held-out classes (SSRF, code injection) on the transfer partition; v39 on SSRF right class 18 of 33.
- Twin concordance and twin ordering from the logged status gap, patch exactness, valid JSON (v39: 193 of 199).
- Languages with fewer than 150 units are reported with their counts and wide intervals, not dropped and not interpreted.

## 5. Addendum: the new-language floors, fixed on 2026-11-11 from the counted held-out views of the final build (no model result exists)

Development views of the build (positive = discovery, clean = negative; python, javascript and typescript are the old flow-proved languages and are judged by section 2 only):
c 252 positive / 387 clean (the only new language with enough views for a floor), cpp 48 / 70, go 48 / 63, java 45 / 65, ruby 33 / 37 (60 / 66 more in transfer), rust 33 / 46,
php 15 / 20, csharp 12 / 16, scala, kotlin, vue 3 each. Diff-proof strengths of the development partition: diff_guard 984 views, diff_sink 87, strict_attribution 152.

The evaluation delivery (built after training, `tools.package_grounded_eval_v2 ... --recipe 12 --same-cohort-as <v39 eval_plan.json> --language-functions c=40,go=8,java=8,ruby=8,rust=8,cpp=8,php=8`)
adds to the unchanged 199-view cohort a deterministic language cohort: for each language without a flow analyser the first N pair-complete functions (vulnerable view and fixed twin in one
prompt form) of the v40 development and transfer partitions in the order of the sha256 of the unit, no preference for a proof strength. Languages with fewer functions give all of them.
Two floors for the new languages, both on the saved answers to that language cohort:

5.1 valid JSON on all language-cohort views: at least 0.90 (v39 on its own cohort: 193 of 199 = 0.97).
5.2 C functions of the language cohort (40 functions, 80 views): the mean of the share of vulnerable views answered "finding" and the share of fixed views answered "no_finding" is at least 0.60
    (a model that flags everything or clears everything scores 0.50 and fails; the class is not part of this floor because every kernel class is read from the commit text, a weaker label).

Every other new language and every proof strength is reported with its counts and Wilson intervals, with no floor, because each has too few held-out functions for a floor (C has 252 positive and 387 clean development views in the dataset, 40 functions in the cohort; the others 8).
The new-language floors do not replace section 2. v40 is accepted as the new baseline only if section 2 holds AND 5.1 AND 5.2 hold. If section 2 holds but 5.1 or 5.2 fails, v39 stays the
baseline for the old classes and v40 is reported as a language-extension result that did not meet its floor.

Where the labels come from, so nobody reads more into a number than it holds: python, javascript, typescript units are flow-proved (analyser); other languages are proved by the diff
(an added guard, a sink word near the change, or the strict attribution of the commit to the unit) and name the class from the advisory's CWE; every Linux kernel (C) unit names its class
from the words of the fix commit through a fixed rule table (`grounded_textclass`, `class_basis: text_rule`) because kernel advisories list no CWE.

## 6. What this rule cannot show

Diff-proved labels are weaker than proved flows and can teach "the line before an added check" instead of the weakness; per-language results rest on few held-out views; no independent
benchmark; no executed verification. It does not support certifying code as safe.

Known risk to section 2, stated before the run, and the control that was added: the first narrow plan (6,000 draws) gave the old origins only 2,526 draws against about 2,900 for v39. The final recipe
sets the worth of a draw for the old flow-proved origins (recipe 12, allocation eta: upstream_fix, sanitiser_removal, unsafe_substitution 1.6, recovered_D 1.1, recovered_L 0.5) and the simulation on the final
dataset (tools.simulate_allocation, 7,800 draws, the training run's own plan_epoch) gives them 3,274 draws, the new diff-proved units 4,170 (diff_guard 3,496 over 874 units, diff_sink 610 over 305 units,
strict_attribution 64): the old classes keep more exposure than v39 had. If section 2 still fails by a small margin, the diff-proved share of the draws is the first suspect and the next recipe caps it
(a new written plan, a new run).
