# Final results (session 2026-08-13) — SUPERSEDED, kept for the ablation methodology

**These numbers are from the original 12-class-provenience corpus, before this
project's later full corpus rebuild** (Proto-Elamite exclusion, train/test
tablet-id leak fix, provenience expansion 12→36 classes) **and retrain.**
`checkpoints_final_text`/`checkpoints_final_vision` on disk now hold the
*retrained* weights, not the ones this file's table describes. For current
numbers see `docs/paper_draft.md`'s Results section and `results_final/README.md`.

This file is kept because the six-run ablation below (which head benefits
from vision) is the methodology later reapplied at the new 36-class scale —
only the specific numbers are stale, the experimental design and its
conclusion (only `provenience_head` should see the image) are not, and were
independently reconfirmed post-rebuild.

Two deliverable models, both mBERT multi-task (period/genre/language/
provenience heads + MLM), trained on the `documents` HF config
(`AlexSychovUN/Enheduanna-Dataset`), 24 epochs, `context_char_min=32
context_char_max=850 max_length=512 batch_size=64 grad_accum=4`.

## Final models

| | `checkpoints_final_text` | `checkpoints_final_vision` |
|---|---|---|
| Image | off | on, `provenience_head` only, `vision_init=finetune` |
| eval_loss | 8.560 | 8.503 |
| period macro_f1 | 0.8614 | 0.8562 |
| genre macro_f1 | 0.8538 | 0.8590 |
| language macro_f1 | 0.7802 | 0.8339 |
| **provenience macro_f1** | **0.7252** | **0.7658** |
| mlm_mrr | 0.7967 | 0.7967 |

`checkpoints_final_vision` is the one to cite as "the" vision model.
`checkpoints_final_text` is the text-only baseline it's compared against.

## Which heads should see the image? (ablation)

Question: does conditioning period/genre on the image help them too, the
way it helps provenience -- especially now that the image pipeline itself
was fixed this session (ImageNet normalize, post-ResNet LayerNorm, wider
Aeneas-matched augmentation; see `train_mbert.py`'s `--image_heads` flag and
git log for those commits)?

Method: a head's own macro_f1 moves by a noticeable amount between ANY two
runs just from random init/data order, even when that head never touches
the image (see the `language` column below -- it's never image-conditioned
in any of these runs, yet swings as much as the heads under test). So a
head only counts as "really helped by vision" if its image-conditioned
score clears the range spanned by its own *unconditioned* scores across the
other runs, not just beats one baseline number.

Six runs total, full training_history.json + training_log.txt for the four
ablation-only ones kept in `docs/ablation_runs/` (their 1.1GB checkpoint
weights were deleted after extracting these -- the conclusion is settled,
nobody needs to reload them):

| run | image_heads | period_f1 | genre_f1 | language_f1 (control) | provenience_f1 |
|---|---|---|---|---|---|
| text_only | none | 0.8614 | 0.8538 | 0.7802 | 0.7252 |
| vision (`checkpoints_final_vision`) | provenience | 0.8562 | 0.8590 | 0.8339 | **0.7658** |
| vision_rerun (`docs/ablation_runs/provenience_run2`) | provenience | 0.8705 | 0.8593 | 0.8032 | **0.7642** |
| vision_genre_only (`docs/ablation_runs/genre_only`) | genre | 0.8761 | 0.8600 | 0.8390 | 0.7360 |
| vision_period_only (`docs/ablation_runs/period_only`) | period | 0.8676 | 0.8603 | 0.8118 | 0.7226 |
| vision_period_genre (`docs/ablation_runs/period_genre`) | period, genre | 0.8714 | 0.8649 | 0.8157 | 0.7212 |

Reading it head by head (bold = the value(s) where that head IS
image-conditioned in that run):

- **provenience**: unconditioned range (text_only, period_only, genre_only,
  period_genre rows) = [0.7212, 0.7360]. Conditioned = 0.7658, 0.7642 in two
  independent runs, 0.0016 apart. No overlap with the unconditioned range at
  all (gap of 0.028+) -- real, reproduced effect.
- **period**: unconditioned range (text_only, vision, vision_rerun,
  genre_only rows) = [0.8562, 0.8761]. Conditioned = 0.8714 (period_genre
  run), 0.8676 (period_only run) -- both comfortably inside the
  unconditioned range. No effect.
- **genre**: unconditioned range (text_only, vision, vision_rerun,
  period_only rows) = [0.8538, 0.8603]. Conditioned = 0.8649 (period_genre
  run, +0.0046 over the range top -- looked like a signal at first) but
  0.8600 isolated alone (period_genre run, `--image_heads genre`) -- inside
  the range. The period_genre run's apparent genre lift didn't reproduce
  once period was removed from the same run; most likely it was leakage
  through the shared vision backbone with period, not an independent genre
  signal. No effect.

**Conclusion**: only `provenience_head` should see the image, matching both
this project's original 4-way ablation (before this session's pipeline
fixes) and Aeneas's own architecture (Assael et al. 2025 restrict vision to
the geography head). `checkpoints_final_vision` is the final vision model
for the thesis.

## Provenience gain across every corpus state (archive, 2026-09-04)

The full trajectory, across every corpus rebuild this project has gone
through, of text-only vs. vision-conditioned provenience macro-F1. Kept
here purely as a record -- `docs/paper_draft.md` and `results_final/README.md`
cite only the current (last row) numbers.

| Corpus state | Documents | Split | text-only F1 | vision F1 | Gain |
|---|---|---|---|---|---|
| 12-class provenience (2026-08-13) | — | validation | 0.725 | 0.766 / 0.764 (two runs) | +0.040 |
| 36-class, pre len(signs)<2 fix (2026-08-24) | 56,934 | test | 0.449 | 0.541 | +0.092 |
| Post len(signs)<2 fix (2026-09-03 AM) | 126,023 | test | 0.569 | 0.633 | +0.064 |
| Post dedup/leakage/bracket fixes (2026-09-03 PM) | 150,804 | test | 0.674 | **0.685** | +0.011 |

**Reading it:** text-only's own provenience macro-F1 rose from 0.449 to
0.674 (+0.225) purely from corpus fixes -- more documents, no lost lines,
no train/test leakage -- while vision's *added* gain over text-only shrank
monotonically across the three 36-class states (+0.092 -> +0.064 -> +0.011).
The likely reading: the cleaner and larger the text corpus gets, the less
headroom is left for a second modality to close, and part of the earlier,
larger gains may specifically have reflected the leaked train/test overlap
disproportionately inflating scores on photographed (and therefore
duplicate-content-prone) tablets rather than a genuine image signal --
plausible, not confirmed. The 12-class point doesn't fit the same monotonic
story, but it's a different label-space scale and a validation- rather than
test-split number, so it isn't directly comparable to the other three.
