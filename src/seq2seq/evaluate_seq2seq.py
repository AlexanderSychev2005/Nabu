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
from tokenizers import Tokenizer
from transformers import T5ForConditionalGeneration

from src.seq2seq.train_seq2seq import BASE_DIR, DEFAULT_REPO_ID, EOS, TASK_DIRS, encode_source


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


def build_allowed_ids(
    sources: list[str], sign_candidates: dict[str, list[int]], eos_id: int, fallback: list[int],
) -> list[list[int]]:
    """Per-example allowed target-token ids: the union of every candidate
    sign's own observed co-occurring tokens (see build_sign_candidates()'s
    docstring for why this is a per-example, not per-position, restriction).
    A sign never seen in training (or an example with no info at all) falls
    back to the full valid-target range rather than banning everything."""
    result = []
    for src in sources:
        allowed = set()
        for c in set(src):
            allowed |= set(sign_candidates.get(c, ()))
        if not allowed:
            allowed = set(fallback)
        allowed.add(eos_id)  # sign_candidates never contains it (see train_seq2seq.py); must stay generatable
        result.append(sorted(allowed))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=list(TASK_DIRS), required=True)
    parser.add_argument("--checkpoint", type=str, default=None)
    parser.add_argument("--n_examples", type=int, default=None,
                         help="cap the number evaluated, for a quick dev-time check; omit to use the whole split")
    parser.add_argument("--eval_batch_size", type=int, default=64)
    parser.add_argument("--max_len", type=int, default=96)
    parser.add_argument("--num_beams", type=int, default=1)
    parser.add_argument("--constrain_by_signs", action="store_true",
                         help="restrict generation per example to target tokens ever seen with one of its "
                              "input signs during training (see build_sign_candidates()'s docstring) -- opt-in, "
                              "since Gordin et al. 2020 found their best (BiLSTM) model did better WITHOUT this "
                              "kind of restriction, so it's not assumed to help here either")
    parser.add_argument("--data_dir", type=str, default=DEFAULT_REPO_ID,
                         help="HF repo id (default), or a local data/processed/hf_dataset_<task> path")
    parser.add_argument("--split", choices=["validation", "test"], default="validation",
                         help="'validation' for dev-time checks (beam size, checkpoint picking); "
                              "'test' ONLY once, for the final reported number -- never to guide choices")
    args = parser.parse_args()

    ckpt = args.checkpoint or os.path.join(BASE_DIR, "checkpoints_seq2seq", args.task, "best")
    source_vocab = json.load(open(os.path.join(ckpt, "source_vocab.json"), encoding="utf-8"))
    target_tokenizer = Tokenizer.from_file(os.path.join(ckpt, "target_tokenizer.json"))
    eos_id, unk_id = target_tokenizer.token_to_id(EOS), target_tokenizer.token_to_id("<unk>")
    target_vocab_size = json.load(open(os.path.join(ckpt, "vocab_meta.json")))["target_vocab_size"]
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = T5ForConditionalGeneration.from_pretrained(ckpt).to(device).eval()
    model.config.use_cache = True  # a checkpoint trained with --grad_checkpointing saved use_cache=False;
    # that only matters for training, generation always wants the KV cache back for speed.
    total_vocab_size = model.config.vocab_size

    # Hard-suppress source-only characters (e.g. cuneiform signs) as
    # generation candidates -- see train_seq2seq.py's module docstring.
    suppress_tokens = list(range(target_vocab_size, total_vocab_size)) or None

    sign_candidates = {}
    if args.constrain_by_signs:
        sign_candidates = json.load(open(os.path.join(ckpt, "sign_candidates.json"), encoding="utf-8"))

    extract = TASK_DIRS[args.task]
    if "/" in args.data_dir and not os.path.exists(args.data_dir):
        ds = load_dataset(args.data_dir, args.task)
    else:
        ds = load_from_disk(args.data_dir)
    pairs = [extract(r) for r in ds[args.split]]
    if args.n_examples is not None:
        pairs = pairs[: args.n_examples]

    total_char_edits = total_ref_chars = 0
    total_word_edits = total_ref_words = 0
    exact = 0
    shown = 0
    n_batches = (len(pairs) + args.eval_batch_size - 1) // args.eval_batch_size
    for bi in range(0, len(pairs), args.eval_batch_size):
        batch = pairs[bi : bi + args.eval_batch_size]
        srcs = [encode_source(s, source_vocab, eos_id, unk_id, args.max_len) for s, _ in batch]
        width = max(len(s) for s in srcs)
        pad_id = target_tokenizer.token_to_id("<pad>")
        input_ids = torch.full((len(srcs), width), pad_id, dtype=torch.long)
        attn_mask = torch.zeros((len(srcs), width), dtype=torch.long)
        for i, s in enumerate(srcs):
            input_ids[i, : len(s)] = torch.tensor(s)
            attn_mask[i, : len(s)] = 1
        input_ids, attn_mask = input_ids.to(device), attn_mask.to(device)

        prefix_allowed_tokens_fn = None
        if args.constrain_by_signs:
            allowed = build_allowed_ids([s for s, _ in batch], sign_candidates, eos_id, list(range(target_vocab_size)))
            num_beams = args.num_beams
            prefix_allowed_tokens_fn = lambda batch_id, sent: allowed[batch_id // num_beams]

        with torch.no_grad():
            gen = model.generate(
                input_ids, attention_mask=attn_mask, max_new_tokens=args.max_len,
                num_beams=args.num_beams, suppress_tokens=suppress_tokens,
                prefix_allowed_tokens_fn=prefix_allowed_tokens_fn,
            )

        for (src, tgt), ids in zip(batch, gen.tolist()):
            pred = target_tokenizer.decode(ids)
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
    print(f"split={args.split} n={n} constrain_by_signs={args.constrain_by_signs}")
    print(f"CER: {100*total_char_edits/total_ref_chars:.1f}%")
    print(f"WER: {100*total_word_edits/total_ref_words:.1f}%")
    print(f"Exact match: {100*exact/n:.1f}%")


if __name__ == "__main__":
    main()
