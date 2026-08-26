"""Small from-scratch character-level seq2seq (T5 architecture) for either
seq2seq side experiment: signs->transliteration or transliteration->English.

Deliberately NOT the BIO-tagging setup Akkademia (Gordin et al. 2020) uses --
see the seq2seq framing discussion: source and target are plain strings,
character-tokenized, so compound-sign / many-to-one alignment never needs
explicit handling (the encoder-decoder attention learns it implicitly).
Every sign in `signs` is already a single Unicode codepoint, so joining the
list with spaces and splitting per character mostly recovers per-sign
tokens anyway, without a separate sign-vocab/embedding path.

One shared character vocabulary covers both source and target text (T5 ties
one vocab_size across encoder input and decoder input/output) -- simplest
correct setup for a from-scratch model at this scale, not a design that
needs the two languages to share meaning, just alphabet.
"""
import argparse
import os
import random
import time

import torch
from datasets import load_dataset, load_from_disk
from torch.utils.data import DataLoader, Dataset
from transformers import T5Config, T5ForConditionalGeneration
from transformers.optimization import get_cosine_schedule_with_warmup

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_REPO_ID = "AlexSychovUN/Nabu-Dataset"
# HF config name == task name for both -- one dict covers both lookups.
TASK_DIRS = {
    "signs_translit": lambda r: (" ".join(r["signs"]), r["text"]),
    "translit_english": lambda r: (r["translit"], r["translation"]),
}

PAD, BOS, EOS, UNK = "<pad>", "<bos>", "<eos>", "<unk>"
SPECIALS = [PAD, BOS, EOS, UNK]


def build_vocab(texts: list[str]) -> dict[str, int]:
    chars = sorted(set("".join(texts)))
    vocab = {tok: i for i, tok in enumerate(SPECIALS)}
    for c in chars:
        vocab[c] = len(vocab)
    return vocab


def encode(text: str, vocab: dict[str, int], max_len: int) -> list[int]:
    ids = [vocab.get(c, vocab[UNK]) for c in text[: max_len - 1]]
    ids.append(vocab[EOS])
    return ids


class PairDataset(Dataset):
    def __init__(self, pairs: list[tuple[str, str]], vocab: dict[str, int], max_len: int) -> None:
        self.pairs = pairs
        self.vocab = vocab
        self.max_len = max_len

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, idx: int) -> tuple[list[int], list[int]]:
        src, tgt = self.pairs[idx]
        return encode(src, self.vocab, self.max_len), encode(tgt, self.vocab, self.max_len)


def collate(batch: list[tuple[list[int], list[int]]], pad_id: int) -> dict[str, torch.Tensor]:
    src_ids, tgt_ids = zip(*batch)
    src_len = max(len(s) for s in src_ids)
    tgt_len = max(len(t) for t in tgt_ids)
    input_ids = torch.full((len(batch), src_len), pad_id, dtype=torch.long)
    attn_mask = torch.zeros((len(batch), src_len), dtype=torch.long)
    labels = torch.full((len(batch), tgt_len), -100, dtype=torch.long)
    for i, (s, t) in enumerate(zip(src_ids, tgt_ids)):
        input_ids[i, : len(s)] = torch.tensor(s)
        attn_mask[i, : len(s)] = 1
        labels[i, : len(t)] = torch.tensor(t)
    return {"input_ids": input_ids, "attention_mask": attn_mask, "labels": labels}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=list(TASK_DIRS), required=True)
    parser.add_argument("--max_len", type=int, default=96)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--max_steps", type=int, default=1500)
    parser.add_argument("--d_model", type=int, default=256)
    parser.add_argument("--num_layers", type=int, default=4)
    parser.add_argument("--num_heads", type=int, default=4)
    parser.add_argument("--out_dir", type=str, default=None)
    parser.add_argument("--save_every", type=int, default=2000)
    parser.add_argument("--warmup_steps", type=int, default=500)
    parser.add_argument("--num_workers", type=int, default=0)
    parser.add_argument("--bf16", action="store_true", help="mixed precision -- MI300X/A100+ only, skip on older cards")
    parser.add_argument("--data_dir", type=str, default=DEFAULT_REPO_ID,
                         help="HF repo id (default), or a local data/processed/hf_dataset_<task> path")
    args = parser.parse_args()

    extract = TASK_DIRS[args.task]
    print(f"Loading '{args.task}' config from {args.data_dir} ...")
    if "/" in args.data_dir and not os.path.exists(args.data_dir):
        ds = load_dataset(args.data_dir, args.task)
    else:
        ds = load_from_disk(args.data_dir)
    train_pairs = [extract(r) for r in ds["train"]]
    val_pairs = [extract(r) for r in ds["validation"]]
    random.Random(0).shuffle(train_pairs)
    print(f"train pairs: {len(train_pairs)}, validation pairs: {len(val_pairs)}")

    vocab = build_vocab([s for s, t in train_pairs] + [t for s, t in train_pairs])
    print(f"shared char vocab size: {len(vocab)}")

    train_ds = PairDataset(train_pairs, vocab, args.max_len)
    loader = DataLoader(
        train_ds, batch_size=args.batch_size, shuffle=True,
        collate_fn=lambda b: collate(b, vocab[PAD]),
        num_workers=args.num_workers, pin_memory=True, persistent_workers=args.num_workers > 0,
    )

    config = T5Config(
        vocab_size=len(vocab), d_model=args.d_model, d_ff=args.d_model * 2,
        num_layers=args.num_layers, num_heads=args.num_heads,
        decoder_start_token_id=vocab[BOS], pad_token_id=vocab[PAD], eos_token_id=vocab[EOS],
    )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = T5ForConditionalGeneration(config).to(device)
    print(f"model params: {sum(p.numel() for p in model.parameters()) / 1e6:.1f}M, device={device}")

    import json

    out_dir = args.out_dir or os.path.join(BASE_DIR, "checkpoints_seq2seq", args.task)
    os.makedirs(out_dir, exist_ok=True)

    def save() -> None:
        model.save_pretrained(out_dir)
        json.dump(vocab, open(os.path.join(out_dir, "vocab.json"), "w", encoding="utf-8"), ensure_ascii=False)
        json.dump(vars(args), open(os.path.join(out_dir, "train_args.json"), "w"))
        print(f"[checkpoint saved at step {step}] {out_dir}", flush=True)

    optim = torch.optim.AdamW(model.parameters(), lr=args.lr)
    scheduler = get_cosine_schedule_with_warmup(optim, args.warmup_steps, args.max_steps)
    amp_dtype = torch.bfloat16 if args.bf16 else None
    model.train()
    step = 0
    t0 = time.time()
    while step < args.max_steps:
        for batch in loader:
            batch = {k: v.to(device) for k, v in batch.items()}
            with torch.autocast(device_type="cuda", dtype=amp_dtype, enabled=args.bf16):
                out = model(**batch)
            out.loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optim.step()
            scheduler.step()
            optim.zero_grad()
            step += 1
            if step % 50 == 0:
                lr_now = scheduler.get_last_lr()[0]
                print(f"step {step}/{args.max_steps} loss={out.loss.item():.3f} lr={lr_now:.2e} ({time.time()-t0:.0f}s)", flush=True)
            if step % args.save_every == 0:
                save()
            if step >= args.max_steps:
                break

    save()
    print(f"Done, saved model + vocab to {out_dir}")


if __name__ == "__main__":
    main()
