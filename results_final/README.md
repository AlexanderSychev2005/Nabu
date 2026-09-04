# Final test-split evaluation (session 2026-09-03, post dedup/leakage-fix retrain)

Numbers below are from the final checkpoints (`checkpoints_final_text`,
`checkpoints_final_vision`) trained after this session's corpus dedup and
split-leakage fixes: a line-deduplication step that had been scoped across
the whole corpus rather than per tablet (silently dropping a tablet's own
content whenever it happened to match another tablet's line verbatim), a
related bug that discarded any fully-bracketed (editorially-restored) line
outright, and a train/test/validation split leak caused by ORACC
occasionally joining several physical P-numbers into one `tablet_id`. These
supersede the numbers from the immediately preceding corpus-completeness-fix
retrain, which in turn superseded the 2026-08-24 rebuild (Proto-Elamite
exclusion, original tablet_id leakage fix, provenience expansion 12→36
classes). First and only time either checkpoint touched the `test` split —
every number quoted earlier (validation, used for checkpoint selection
during training) is a different, slightly less strict split. This is the
number to actually cite.

## Files

- `metrics_text.json` — `checkpoints_final_text` on `test` (7,508 tablets):
  aggregate metrics (MLM MRR/Hit@k/CER, per-head accuracy + macro-F1) and a
  full per-class precision/recall/F1/support breakdown for all 4 metadata
  heads.
- `metrics_vision.json` — same, `checkpoints_final_vision` (provenience
  image-conditioned), same 7,508-tablet `test` split (tablets without a
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
  — 12 hand-picked tablets (Gilgamesh/Enuma Elish/Atrahasis/Hammurabi + P387407,
  plus Enheduanna's Exaltation of Inanna and dedicatory disc -- represented
  twice, once as the RIME scholarly composite (P461942, translation but no
  photo) and once as an actual photographed exemplar (P217330, Penn Museum,
  RIME 2.01.01.16 ex. 01 -- the real disc, no translation but a real photo
  and line table)). Each example has:
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
  bug: `predictions_demo_showcase.md` got a description+line-table for 9 of
  its 12 examples (the 9 with a real photo, P217330 among them); only 3 have
  any translation -- P387407 (an ordinary letter), plus Enheduanna's
  Exaltation of Inanna (P346194) and dedicatory disc (P461942, the RIME
  composite -- its photographed twin P217330 has the line table and photo
  but no translation, since CDLI's translation field is attached to the
  composite entry, not the individual exemplar). The remaining 7 are
  eBL-sourced literary fragments (Gilgamesh etc.) or the Hammurabi stele
  (P249253) with no CDLI inscription record at all and no translation field
  in eBL's own API either (checked both live). `predictions_demo.md`'s
  random 20 got line tables for 13/20 (the ones with a photo) and 0/20 have
  a translation this round -- a coincidence of which random tablets a fixed
  seed draws from a corpus whose test split keeps shifting as the corpus
  itself gets rebuilt, not a regression; translation coverage for
  administrative/lexical texts (the bulk of a random sample) was always
  thin. Same wall this session already hit with K.3375: open translations
  for literary works don't really exist outside copyrighted scholarly
  editions.
- `demo_images/` — `<tablet_id>.jpg` (model-input crop) and
  `<tablet_id>_full.jpg` (full original, reference only) for every example
  with a photo in either demo file.

## Headline numbers (test split, not validation)

| | untrained (no finetuning) | text-only | vision (provenience) |
|---|---|---|---|
| mlm_mrr | 0.481 | 0.865 | 0.865 |
| mlm_acc (top-1) | 0.436 | 0.820 | 0.820 |
| mlm_top3_acc | 0.497 | 0.895 | 0.895 |
| mlm_top5_acc | 0.525 | 0.920 | 0.919 |
| mlm_cer | 0.676 | 0.195 | 0.196 |
| period acc / macro-F1 | 0.454 / 0.079 | 0.958 / 0.880 | 0.962 / 0.895 |
| genre acc / macro-F1 | 0.054 / 0.030 | 0.961 / 0.904 | 0.962 / 0.908 |
| language acc / macro-F1 | 0.033 / 0.028 | 0.985 / 0.843 | 0.986 / 0.839 |
| **provenience acc / macro-F1** | 0.003 / 0.002 | 0.862 / **0.674** | 0.870 / **0.685** |

`mlm_cer` is a pooled character-level edit-distance ratio over every masked
position's top-1 restoration (see `evaluate_mbert.py`'s `mlm_cer` /
`docs/paper_draft.md`'s Methods section for the exact definition and why it
is *not* stratified by masked-span length the way Aeneas/Ithaca's own CER
is — our masking is standard random-15%-of-tokens MLM, not a designed span
of chosen character length, so their length-stratification doesn't apply
here as-is).

The untrained column confirms neither result is free: mBERT's own
pretraining gets restoration to a non-trivial MRR 0.48 / CER 0.68 zero-shot
(matching Lazar et al. 2021's own point that multilingual pretraining
transfers usefully to Akkadian on its own), but the metadata heads are
exactly what random Linear-layer init on a 4/6/9/36-class problem looks
like -- effectively chance, several provenience classes collapsed to 0 F1
(see `metrics_untrained.json`'s per-class breakdown). All four heads' real
signal comes entirely from this project's finetuning, not from the backbone
alone.

Provenience macro-F1 gains +0.011 (0.674→0.685) with vision on this
corpus revision -- a real gain, and the vision-conditioned checkpoint has
now come out ahead of the text-only one in every one of four separate
retrainings across successive corpus states (see `docs/paper_draft.md`
Section 5 for the full history: +0.092 on the original 36-class rebuild,
+0.064 on the further-expanded one, +0.011 here). The margin has narrowed
at each successive corpus fix; we report the current number as the one to
cite, on the view that a smaller effect on a corpus with its known
data-quality issues resolved is more trustworthy than the larger effect
measured earlier. `mlm_cer` is essentially unchanged between the two
checkpoints (0.195 vs 0.196), as expected: the image only reaches the
provenience head, never the restoration head, so restoration performance
should not depend on whether the checkpoint was trained with or without
vision conditioning.

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
  --context_char_max 850 --max_length 512 --batch_size 2 \
  --output_file results_final/metrics_text.json

uv run python src/analysis/evaluate_mbert.py \
  --checkpoint checkpoints_final_vision/final_model \
  --data_dir AlexSychovUN/Enheduanna-Dataset --hf_config documents --split test \
  --context_char_max 850 --max_length 512 --batch_size 2 \
  --use_image --vision_init finetune --images_from_hf \
  --output_file results_final/metrics_vision.json

uv run python src/analysis/evaluate_mbert.py \
  --checkpoint checkpoints_final_text/final_model --untrained \
  --data_dir AlexSychovUN/Enheduanna-Dataset --hf_config documents --split test \
  --context_char_max 850 --max_length 512 --batch_size 2 \
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
  --tablet_ids "P273207,P285823,P273223,P402919,ebl:BM.42004,P404643,P402685,P387407,P346194,P461942,P217330,P249253" \
  --context_char_max 850 --max_length 512 --embed_images --fetch_cdli_info \
  --output_file results_final/predictions_demo_showcase.md
```

`--batch_size 2` is sized for this machine's 4GB GPU (the training runs
used a larger box) -- bump it up if running elsewhere. `--embed_images`
needs the local raw-image cache (`data/vision_dataset/`,
`data/raw/cuneiml/images_full/`) for the full-resolution photos -- the
224x224 crops alone come from the HF `vision` config regardless.
