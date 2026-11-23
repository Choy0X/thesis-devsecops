# Training record

Corpus sizes and draw counts refer to the datasets used for each run. A draw is one scheduled training example. A spot check is reported separately from the later matched-view evaluation.

## Training runs

| Version | Recipe | Corpus | Updates (draws) | Evaluation and outcome |
| --- | --- | --- | --- | --- |
| v30 | 4 | first grounded set (v30) | 39 | spot check: valid JSON 12/12 (original 2/12), findings 7/8, repairs 0/2 |
| v33 | 5 | 100; 196 | 45 | spot check: JSON 11/12, findings 6/8, SSRF 0/3; later cleared 5/22 clean views |
| v34 | 6 | v33 units plus class-confusion negatives | 40 | spot check: class 3/6, negatives 3/6; later cleared 18/22, class 6/25 |
| v36 | 8 | 313 (93 real); 1,325 | 397 of 397 (3,176) | reference arm, 86 matched views: fixed cleared 35/40, pair test 25/40; 53 views shared with v33 and v34: clean cleared 20/22, class 12/25, patch exact 3/6 |
| v37 | 9 | 1,362; 6,868 | 142 of 168 (1,136) | 74 matched views: worse than v36 on 8 functions, better on 1 |
| v38 | 10 | 1,238; 6,161 | 447 of 447 (3,576) | rule not met: fixed cleared 27/40 (v36 35), pair test 12/40 (v36 25) |
| v39 | 11 | 1,048; 4,838 | 600 of 600 (3,600 discovery) | rule not met: fixed cleared 33/40 (needed 35), hard failures 6 (v36 1); kept as the baseline (acceptance rule) |
| v40a | 12 | 3,572; 16,732 | 1,009 of 1,101 (8,072) | rule met: fixed cleared 36/40, pair test 32/40; accepted |
| v40b | 13 | as v40a | 998 of 998 (7,984) | rule not met: pair test 24/40; not accepted |

## Final model settings

| Group | Setting |
| --- | --- |
| Model shape | 28 layers, hidden size 3,584, 28 attention heads, 4 key-value heads, vocabulary 152,064 |
| Adapter | no LoftQ initialization |
| Loss | prompt and padding masked, end-of-sequence supervised; pairwise rank term and decision head off |
| Batching | no sequence packing; length grouping; ceiling 1,300 updates, planned 1,101 (first stage) and 998 (second stage) |
| Context | an overlong view is rebuilt or quarantined, never truncated; generation limit 512 tokens (repair 896) |
| Seeds | 17 (first stage), 29 (second stage), from the configured 17, 29 and 43 |
| Tokens | 16,732 of 16,732 training views fit; 28,658,004 prompt and 2,034,746 completion tokens |

## Training recipes

| Recipe | Corpus | Draws per 8 (discovery / repair / test) | Warmup, peak rate, ceiling | Pairing | Allocation of discovery draws |
| --- | --- | --- | --- | --- | --- |
| 4 | v30 | 3 / 3 / 2 | 3, 8e-5, 64 | none | none |
| 5 | v33 | 7 / 1 / 0 | 3, 8e-5, 64 | none | none |
| 6 | v34 | 6 / 2 / 0 | 3, 8e-5, 64 | none | none |
| 7 | v35 | 6 / 2 / 0 | 8, 6e-5, 640 | none | none |
| 8 | v36 | 7 / 1 / 0 | 8, 6e-5, 640 | none | none |
| 9 | v37 | 7 / 1 / 0 | 8, 6e-5, 640 | none | none |
| 10 | v38 | 6 / 2 / 0 | 8, 6e-5, 640 | by update | none |
| 11 | v39 | 6 / 2 / 0 | 8, 6e-5, 600 | planned | water-fill: tau 4, d_max 12, lone share 0.2, 3,600 draws; twin-margin loss weight 0.5, margin 1.7 |
| 12 | v40 | 6 / 2 / 0 | 8, 6e-5, 1,300 | planned | as 11 with 7,800 draws and raised worths |
| 13 | v40 | 6 / 2 / 0 | 8, 3e-5, 1,300 | planned | as 12; seed 29; starts from the first stage's adapter |

## Sampling by origin

| Origin | Default worth | Recipe 12 worth | Units | Draws | Draws per unit |
| --- | --- | --- | --- | --- | --- |
| upstream_fix | 1.0 | 1.6 | 226 | 2,366 | 10.5 |
| sanitiser_removal | 1.0 | 1.6 | 30 | 283 | 9.4 |
| unsafe_substitution | 1.0 | 1.6 | 24 | 235 | 9.8 |
| recovered_D | 0.7 | 1.1 | 38 | 228 | 6.0 |
| recovered_L | 0.4 | 0.5 | 81 | 162 | 2.0 |
| diff_guard | 0.7 | 0.7 | 874 | 3,496 | 4.0 |
| diff_sink | 0.55 | 0.55 | 305 | 610 | 2.0 |
| strict_attribution | 0.4 | 0.4 | 561 | 64 | 0.11 |
