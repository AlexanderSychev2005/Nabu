# Final test-split evaluation (session 2026-09-03, post corpus-completeness-fix retrain)

Numbers below are from the final checkpoints (`checkpoints_final_text`,
`checkpoints_final_vision`) trained after this session's corpus-completeness
fix (a `len(signs)<2` bug had been silently discarding ORACC sub-editions
that carried only a normalized reading with no sign-level glyph data, even
though their transliteration was perfectly usable) — nearly doubling the
document count on top of the earlier 2026-08-24 rebuild (Proto-Elamite
exclusion, train/test tablet_id leakage fix, provenience expansion 12→36
classes), which these numbers also supersede. First and only time either
checkpoint touched the `test` split — every number quoted earlier (validation,
used for checkpoint selection during training) is a different, slightly less
strict split. This is the number to actually cite.

## Files

- `metrics_text.json` — `checkpoints_final_text` on `test` (5,936 tablets):
  aggregate metrics (MLM MRR/Hit@k/CER, per-head accuracy + macro-F1) and a
  full per-class precision/recall/F1/support breakdown for all 4 metadata
  heads.
- `metrics_vision.json` — same, `checkpoints_final_vision` (provenience
  image-conditioned), same 5,936-tablet `test` split (tablets without a
  photo run through the vision model on the same all-zero placeholder image
  used in training).
- `metrics_untrained.json` — the zero-finetuning baseline: plain
  `bert-base-multilingual-cased`'s own pretrained weights (untouched) +
  freshly random-initialized metadata heads, evaluated the same way on the
  same `test` split. Uses `checkpoints_final_text/final_model`'s tokenizer
  (for the injected Akkadian WordPiece tokens + damage sentinels, so
  masking-eligibility and vocab fragmentation match the trained runs
  exactly) but none of its trained weights (`evaluate_mbert.py --untrained`).
  This is the same kind of comparison Lazar et al. 2021 report (their
  Table 2, finetuned vs. non-finetuned mBERT) -- shows how much of the
  result is from Akkadian-specific finetuning vs. mBERT's own multilingual
  pretraining.
- `predictions_demo.md` — 20 random test-split tablets. `predictions_demo_showcase.md`
  — 11 hand-picked tablets (Gilgamesh/Enuma Elish/Atrahasis/Hammurabi + P387407,
  plus Enheduanna's Exaltation of Inanna, Temple Hymns, and dedicatory disc).
  Each example has:
  - a one-line **description** (genre/period/collection/publication, pulled
    from whichever of eBL/CDLI actually has this tablet);
  - a **side-by-side block**: the 224x224 crop the model actually sees + the
    full original photo (all photographed faces, downscaled to 900px longer
    side) on the left, and a genuine **line-by-line table** (cuneiform +
    transliteration + English translation, by ATF line number/face) on the
    right -- a real per-line parse of the tablet's own raw ATF, not this
    project's own flattened whole-document `text`/`signs` columns (which
    lose line boundaries in the corpus merge);
  - the flattened original transliteration, whole-document cuneiform signs,
    and whole-document translation (same content as the line table, joined)
    for reference/searchability;
  - the masked input and both models' top-1/top-3 restoration guess per
    masked token, and both models' metadata predictions (confidence) vs.
    ground truth -- provenience rows where the two models disagree are
    flagged `<- differs`.

  Description/translation/line-table coverage is honestly uneven, not a
  bug: `predictions_demo_showcase.md` got a description+line-table for 8 of
  its 11 examples (the 8 with a real photo); only 3 have any translation --
  P387407 (an ordinary letter), plus Enheduanna's Exaltation of Inanna
  (P346194) and dedicatory disc (P461942), both of which have a CDLI
  translation despite one of them (the disc) lacking a photo. The remaining
  7 are eBL-sourced literary fragments (Gilgamesh etc.) or the Hammurabi
  stele (P249253) with no CDLI inscription record at all and no translation
  field in eBL's own API either (checked both live). `predictions_demo.md`
  did better on translations (3/20, ordinary administrative/lexical texts)
  and got line tables for 18/20. Same wall this session already hit with
  K.3375: open translations for literary works don't really exist outside
  copyrighted scholarly editions.
- `demo_images/` — `<tablet_id>.jpg` (model-input crop) and
  `<tablet_id>_full.jpg` (full original, reference only) for every example
  with a photo in either demo file.

## Headline numbers (test split, not validation)

| | untrained (no finetuning) | text-only | vision (provenience) |
|---|---|---|---|
| mlm_mrr | 0.501 | 0.832 | 0.831 |
| mlm_acc (top-1) | 0.453 | 0.778 | 0.778 |
| mlm_top3_acc | 0.520 | 0.867 | 0.867 |
| mlm_top5_acc | 0.548 | 0.896 | 0.896 |
| mlm_cer | 0.658 | 0.246 | 0.245 |
| period acc / macro-F1 | 0.353 / 0.069 | 0.938 / 0.851 | 0.937 / 0.843 |
| genre acc / macro-F1 | 0.051 / 0.028 | 0.937 / 0.854 | 0.938 / 0.856 |
| language acc / macro-F1 | 0.054 / 0.038 | 0.983 / 0.828 | 0.984 / 0.820 |
| **provenience acc / macro-F1** | 0.003 / 0.004 | 0.743 / **0.569** | 0.758 / **0.633** |

`mlm_cer` is a pooled character-level edit-distance ratio over every masked
position's top-1 restoration (see `evaluate_mbert.py`'s `mlm_cer` /
`docs/paper_draft.md`'s Methods section for the exact definition and why it
is *not* stratified by masked-span length the way Aeneas/Ithaca's own CER
is — our masking is standard random-15%-of-tokens MLM, not a designed span
of chosen character length, so their length-stratification doesn't apply
here as-is).

The untrained column confirms neither result is free: mBERT's own
pretraining gets restoration to a non-trivial MRR 0.50 / CER 0.66 zero-shot
(matching Lazar et al. 2021's own point that multilingual pretraining
transfers usefully to Akkadian on its own), but the metadata heads are
exactly what random Linear-layer init on a 4/6/9/36-class problem looks
like -- effectively chance, several provenience classes collapsed to 0 F1
(see `metrics_untrained.json`'s per-class breakdown). All four heads' real
signal comes entirely from this project's finetuning, not from the backbone
alone.

Provenience macro-F1 gains +0.064 (0.569→0.633) with vision, replicating the
central finding again on this further-expanded, corpus-completeness-fixed
corpus (see `docs/paper_draft.md` Section 5 for the earlier, smaller-corpus
version of this same result -- both the original 12-class-provenience run
and the 2026-08-24 36-class rebuild gave a larger +0.092 gain, so the effect
persists at this scale but with a narrower margin). Language macro-F1 --
which never receives the image in any run, the noise-floor head -- moves by
-0.008; period moves by -0.008 and genre by +0.002, both similarly small.
`mlm_cer` moves by essentially nothing (0.246→0.245) between the two
checkpoints, as expected: the image only reaches the provenience head, never
the restoration head, so restoration performance should not depend on
whether the checkpoint was trained with or without vision conditioning.

## Reading the demo files

- Masking always shows literal `[MASK]` at every chosen position (not
  BERT's real 80/10/10 masking recipe) -- for legibility; the metrics above
  come from the real collator, these files are illustration only.
- The image only reaches `provenience_head` (see
  `src/training/train_mbert.py`'s `MBertMultiTask.forward` -- `mlm_logits`
  is computed before any image concatenation), so restoration differences
  between the two models come only from being separately trained weights,
  not from the image directly. The `provenience` row in each example's
  metadata table is where the image can actually change an answer.
- The full-resolution photo is pulled from the local raw-download cache
  (same source `finalize_vision_crops.py` crops from), not from the model's
  actual input -- it's there for human reference/the diploma writeup, the
  model only ever sees the 224x224 crop next to it.
- `predictions_demo.md`: 13/20 examples used a tablet with a real photo
  (biased toward this on purpose -- otherwise the vision model would run on
  the same all-zero placeholder as text-only most of the time, showing no
  possible difference by construction). Of the examples with a real
  ground-truth provenience label, the two models mostly agree (matching or
  both wrong), disagreeing on a small minority -- consistent with the
  aggregate result being a real but modest average effect, not an
  every-single-example win.

## Reproduce

```bash
uv run python src/analysis/evaluate_mbert.py \
  --checkpoint checkpoints_final_text/final_model \
  --data_dir AlexSychovUN/Enheduanna-Dataset --hf_config documents --split test \
  --context_char_max 850 --max_length 512 --batch_size 4 \
  --output_file results_final/metrics_text.json

uv run python src/analysis/evaluate_mbert.py \
  --checkpoint checkpoints_final_vision/final_model \
  --data_dir AlexSychovUN/Enheduanna-Dataset --hf_config documents --split test \
  --context_char_max 850 --max_length 512 --batch_size 4 \
  --use_image --vision_init finetune --images_from_hf \
  --output_file results_final/metrics_vision.json

uv run python src/analysis/evaluate_mbert.py \
  --checkpoint checkpoints_final_text/final_model --untrained \
  --data_dir AlexSychovUN/Enheduanna-Dataset --hf_config documents --split test \
  --context_char_max 850 --max_length 512 --batch_size 4 \
  --output_file results_final/metrics_untrained.json

uv run python src/analysis/demo_predictions.py \
  --text_checkpoint checkpoints_final_text/final_model \
  --vision_checkpoint checkpoints_final_vision/final_model \
  --data_dir AlexSychovUN/Enheduanna-Dataset --hf_config documents --split test \
  --n_examples 20 --context_char_max 850 --max_length 512 --embed_images --fetch_cdli_info --seed 42 \
  --output_file results_final/predictions_demo.md

uv run python src/analysis/demo_predictions.py \
  --text_checkpoint checkpoints_final_text/final_model \
  --vision_checkpoint checkpoints_final_vision/final_model \
  --data_dir AlexSychovUN/Enheduanna-Dataset --hf_config documents --split test \
  --tablet_ids "P273207,P285823,P273223,P402919,ebl:BM.42004,P404643,P402685,P387407,P346194,P461942,P249253" \
  --context_char_max 850 --max_length 512 --embed_images --fetch_cdli_info \
  --output_file results_final/predictions_demo_showcase.md
```

`--batch_size 4` is sized for this machine's 4GB GPU (the training runs
used a larger box) -- bump it up if running elsewhere. `--embed_images`
needs the local raw-image cache (`data/vision_dataset/`,
`data/raw/cuneiml/images_full/`) for the full-resolution photos -- the
224x224 crops alone come from the HF `vision` config regardless.
