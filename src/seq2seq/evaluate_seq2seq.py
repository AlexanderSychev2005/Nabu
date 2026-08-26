"""CER/WER/exact-match evaluation for a trained seq2seq checkpoint.

Deliberately NOT Gordin et al. 2020's per-sign tagging accuracy -- that
metric requires strict 1:1 input/output position alignment, which our
seq2seq framing does not have (see the seq2seq vs. tagging discussion).
CER/WER are the standard substitutes for comparing two free-length strings.
"""
import argparse
import json
import os

import torch
from datasets import load_dataset, load_from_disk
from transformers import T5ForConditionalGeneration

from src.seq2seq.train_seq2seq import BASE_DIR, BOS, DEFAULT_REPO_ID, EOS, PAD, TASK_DIRS, encode


def levenshtein(a: list, b: list) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = curr
    return prev[-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=list(TASK_DIRS), required=True)
    parser.add_argument("--checkpoint", type=str, default=None)
    parser.add_argument("--n_examples", type=int, default=None,
                         help="cap the number evaluated, for a quick dev-time check; omit to use the whole split")
    parser.add_argument("--eval_batch_size", type=int, default=64)
    parser.add_argument("--max_len", type=int, default=96)
    parser.add_argument("--num_beams", type=int, default=1)
    parser.add_argument("--data_dir", type=str, default=DEFAULT_REPO_ID,
                         help="HF repo id (default), or a local data/processed/hf_dataset_<task> path")
    parser.add_argument("--split", choices=["validation", "test"], default="validation",
                         help="'validation' for dev-time checks (beam size, checkpoint picking); "
                              "'test' ONLY once, for the final reported number -- never to guide choices")
    args = parser.parse_args()

    ckpt = args.checkpoint or os.path.join(BASE_DIR, "checkpoints_seq2seq", args.task, "best")
    vocab = json.load(open(os.path.join(ckpt, "vocab.json"), encoding="utf-8"))
    id2char = {i: c for c, i in vocab.items()}
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = T5ForConditionalGeneration.from_pretrained(ckpt).to(device).eval()

    # Hard-suppress source-only characters (e.g. cuneiform signs) as
    # generation candidates -- see build_vocab()'s docstring. Older
    # checkpoints predate vocab_meta.json; fall back to no suppression
    # rather than failing to load them.
    meta_path = os.path.join(ckpt, "vocab_meta.json")
    target_vocab_size = json.load(open(meta_path))["target_vocab_size"] if os.path.exists(meta_path) else len(vocab)
    suppress_tokens = list(range(target_vocab_size, len(vocab))) or None

    extract = TASK_DIRS[args.task]
    if "/" in args.data_dir and not os.path.exists(args.data_dir):
        ds = load_dataset(args.data_dir, args.task)
    else:
        ds = load_from_disk(args.data_dir)
    pairs = [extract(r) for r in ds[args.split]]
    if args.n_examples is not None:
        pairs = pairs[: args.n_examples]

    def decode(ids: list) -> str:
        out = []
        for i in ids:
            c = id2char.get(int(i), "")
            if c in (PAD, EOS):
                break
            if c == BOS:
                continue
            out.append(c)
        return "".join(out)

    total_char_edits = total_ref_chars = 0
    total_word_edits = total_ref_words = 0
    exact = 0
    shown = 0
    n_batches = (len(pairs) + args.eval_batch_size - 1) // args.eval_batch_size
    for bi in range(0, len(pairs), args.eval_batch_size):
        batch = pairs[bi : bi + args.eval_batch_size]
        srcs = [encode(s, vocab, args.max_len) for s, _ in batch]
        width = max(len(s) for s in srcs)
        input_ids = torch.full((len(srcs), width), vocab[PAD], dtype=torch.long)
        attn_mask = torch.zeros((len(srcs), width), dtype=torch.long)
        for i, s in enumerate(srcs):
            input_ids[i, : len(s)] = torch.tensor(s)
            attn_mask[i, : len(s)] = 1
        input_ids, attn_mask = input_ids.to(device), attn_mask.to(device)

        with torch.no_grad():
            gen = model.generate(
                input_ids, attention_mask=attn_mask, max_new_tokens=args.max_len,
                num_beams=args.num_beams, suppress_tokens=suppress_tokens,
            )

        for (src, tgt), ids in zip(batch, gen.tolist()):
            pred = decode(ids)
            total_char_edits += levenshtein(list(pred), list(tgt))
            total_ref_chars += max(len(tgt), 1)
            pred_words, tgt_words = pred.split(), tgt.split()
            total_word_edits += levenshtein(pred_words, tgt_words)
            total_ref_words += max(len(tgt_words), 1)
            exact += int(pred.strip() == tgt.strip())
            if shown < 8:
                print(f"  src:  {src}\n  ref:  {tgt}\n  pred: {pred}\n")
                shown += 1

        if (bi // args.eval_batch_size + 1) % 20 == 0:
            print(f"  ...batch {bi // args.eval_batch_size + 1}/{n_batches}", flush=True)

    n = len(pairs)
    print(f"split={args.split} n={n}")
    print(f"CER: {100*total_char_edits/total_ref_chars:.1f}%")
    print(f"WER: {100*total_word_edits/total_ref_words:.1f}%")
    print(f"Exact match: {100*exact/n:.1f}%")


if __name__ == "__main__":
    main()
