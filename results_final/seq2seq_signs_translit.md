# Side experiment: signs -> transliteration (seq2seq)

Companion to the main restoration+attribution model -- a from-scratch
character-level T5 that reads a line's Unicode cuneiform signs and predicts
its transliteration, motivated by and compared against Gordin et al. 2020
(Akkademia). Code in `src/seq2seq/`; data is the `signs_translit` config on
`AlexSychovUN/Enheduanna-Dataset` (612,224 line pairs -- train 550,263 /
validation 31,293 / test 30,668 -- no new annotation needed, derived from
the same corpus via `cuneiform_unicode.py`'s existing sign conversion, split
by the same authoritative tablet_id -> split map as `documents`/`vision`).
Retrained (session 2026-09-03) after this project's corpus-completeness fix
(Section 3 of `docs/paper_draft.md`); numbers below supersede the earlier
run's, which used the same architecture and hyperparameters on the
pre-fix corpus.

## Why seq2seq, not Akkademia's tagging setup

Gordin et al. 2020 frame this as a tagging problem (one output label per
input sign, BIO-tagged for compound signs) and report their best model
(BiLSTM) at 97.8% per-sign transliteration accuracy on 23,526 lines. We use
a seq2seq framing instead -- source and target as plain strings, no
positional alignment required -- so compound-sign correspondence is learned
implicitly by attention rather than requiring the alignment machinery their
tagging setup needs. **This means our metric (CER/WER on free-length
strings) is not directly comparable to their 97.8% (a per-position tagging
accuracy)** -- reported here for context, not as a head-to-head number.

## Final configuration and result

T5, character-level, from scratch. Source (signs) and target
(transliteration) get disjoint id ranges rather than one shared vocab --
they're near-disjoint alphabets (387 vs 81 unique characters measured on a
20k-example sample, only 8 shared), so a shared softmax would waste most of
the decoder's output distribution on characters that can never be a valid
transliteration character.

| | |
|---|---|
| Architecture | T5, d_model=512, 6 layers, 8 heads, d_ff=2048 (5.6M params) |
| Training | 8,000 steps, batch 2048, bf16, cosine LR (warmup 300), AMD MI300X |
| Checkpoint selection | best of top-3 by validation loss (not last-step) |
| Decoding | beam search, num_beams=5 |

**Test set (30,668 lines, held out, never used for training or checkpoint selection):**

| Metric | Value |
|---|---|
| CER | 12.3% |
| WER | 28.6% |
| Exact match | 44.1% |

## Experiments tried and retired (not shipped)

Both ablations below were run on the pre-corpus-completeness-fix data and
checkpoint (the 13.0/28.6/44.7 baseline, not the current shipped 12.3/28.6/44.1
one); they were never rerun on the current corpus, since neither changed the
shipped decision. Read their deltas as relative to that earlier baseline, not
the current one.

**Bigger model + BPE target tokenization.** d_model 512->768, layers 6->8,
heads 8->12 (5.6M -> 133.7M params, ~4x), plus byte-level BPE instead of
character-level for the target side (ATF text is built from recurring
syllables/words, so subword units seemed likely to help). Result: CER
12.2%, WER 27.8%, exact match 45.4% -- a real but marginal gain (~0.7-0.8pp
across all three metrics) for roughly 3x the training cost, and a
meaningfully worse train/validation loss gap (val_loss 0.309 vs train loss
~0.12 at step 7000) than the shipped config ever showed, i.e. the bigger
model shows a real overfitting signal without a proportionate generalization
gain. Retired in favor of the smaller, cheaper, already-good-enough config
above. (BPE alone, without the bigger model, was not isolated -- the two
were only ever tested together -- so it's not established that BPE
specifically was the source of the small gain rather than the extra
capacity.)

**Sign-based candidate restriction at generation** (`--constrain_by_signs`
in `evaluate_seq2seq.py`, still available as an opt-in flag). A per-example
approximation of Gordin et al.'s own per-sign candidate-dictionary
restriction (used by their HMM/MEMM, deliberately *not* used by their best
model, BiLSTM): for each test line, restrict the beam to target tokens ever
observed co-occurring with one of its input signs anywhere in training.
Result on the shipped (512/6/8, no BPE) checkpoint: CER 12.3%, WER 28.0%,
exact match 45.3% -- essentially unchanged, marginally worse on 2 of 3
metrics. **Replicates Gordin et al.'s own finding that restricting the
candidate space does not help their best (least-constrained) model,** now
confirmed on an architecturally different seq2seq setup. Not used for the
shipped result.

## Reproduction

```bash
python -m src.seq2seq.train_seq2seq \
  --d_model 512 --num_layers 6 --num_heads 8 \
  --batch_size 2048 --max_steps 8000 --warmup_steps 300 \
  --save_every 1000 --keep_best 3 --num_workers 12 --bf16

python -m src.seq2seq.evaluate_seq2seq --split test --num_beams 5 --eval_batch_size 512
```
