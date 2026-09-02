"""Small from-scratch character-level seq2seq (T5 architecture) for the
signs -> transliteration side experiment.

Deliberately NOT the BIO-tagging setup Akkademia (Gordin et al. 2020) uses --
see the seq2seq framing discussion: source and target are plain strings,
character-tokenized, so compound-sign / many-to-one alignment never needs
explicit handling (the encoder-decoder attention learns it implicitly).
Every sign in `signs` is already a single Unicode codepoint, so joining the
list with spaces and splitting per character mostly recovers per-sign
tokens anyway, without a separate sign-vocab/embedding path.

Source and target don't share an id space (see build_vocab) -- they're
near-disjoint alphabets (measured on this corpus: 387 source chars, 81
target chars, only 8 shared), so a single shared vocab_size would waste
most of the decoder's output softmax on characters (cuneiform signs) that
can never be a valid transliteration character.

(A parallel translit_english side experiment and a BPE/bigger-model variant
of this one were both tried and retired: BPE + a ~4x bigger model gained
~0.8pp CER over this config at ~3x the training cost and a real train/val
overfitting gap, and translit_english's own char-level seq2seq stayed
undertrained on the ~97k pairs available -- see results_final/ for the
numbers. This file matches the configuration that was actually kept.)

Checkpointing/logging follows the same convention as train_mbert.py: a
timestamped training_log_*.txt (file + stdout) under --out_dir, and periodic
checkpoints -- except here selection is by held-out validation loss (this
loop has no HF Trainer to hand that logic to), keeping only the best
--keep_best of them on disk (each is a full copy, not worth accumulating
one per save_every) plus a final best/ copy once training ends. A step
saved because it was competitive at the time can later be pruned if a
later, better step displaces it -- last-step-wins is not assumed correct.
"""
import argparse
import json
import logging
import os
import random
import shutil
import time

import torch
from datasets import load_dataset, load_from_disk
from torch.utils.data import DataLoader, Dataset
from transformers import T5Config, T5ForConditionalGeneration
from transformers.optimization import get_cosine_schedule_with_warmup

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_REPO_ID = "AlexSychovUN/Enheduanna-Dataset"
HF_CONFIG = "signs_translit"


def extract_pair(r: dict) -> tuple[str, str]:
    return " ".join(r["signs"]), r["text"]


PAD, BOS, EOS, UNK = "<pad>", "<bos>", "<eos>", "<unk>"
SPECIALS = [PAD, BOS, EOS, UNK]


def build_vocab(target_texts: list[str], source_texts: list[str]) -> tuple[dict[str, int], int]:
    """Target-alphabet characters get the lowest ids (right after the
    specials), source-only characters are appended after. Returns (vocab,
    target_vocab_size) -- the boundary below which every id is a real
    candidate output, used to hard-suppress the rest at generation time
    (see evaluate_seq2seq.py's suppress_tokens)."""
    target_chars = sorted(set("".join(target_texts)))
    source_only_chars = sorted(set("".join(source_texts)) - set(target_chars))
    vocab = {tok: i for i, tok in enumerate(SPECIALS)}
    for c in target_chars:
        vocab[c] = len(vocab)
    target_vocab_size = len(vocab)
    for c in source_only_chars:
        vocab[c] = len(vocab)
    return vocab, target_vocab_size


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


def setup_logging(out_dir: str) -> logging.Logger:
    os.makedirs(out_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(out_dir, f"training_log_{timestamp}.txt")),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger(__name__)


@torch.no_grad()
def evaluate_loss(model: torch.nn.Module, loader: DataLoader, device: str, amp_dtype) -> float:
    model.eval()
    total_loss, n_batches = 0.0, 0
    for batch in loader:
        batch = {k: v.to(device) for k, v in batch.items()}
        with torch.autocast(device_type="cuda", dtype=amp_dtype, enabled=amp_dtype is not None):
            out = model(**batch)
        total_loss += out.loss.item()
        n_batches += 1
    model.train()
    return total_loss / max(n_batches, 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_len", type=int, default=96)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--max_steps", type=int, default=1500)
    parser.add_argument("--d_model", type=int, default=256)
    parser.add_argument("--num_layers", type=int, default=4)
    parser.add_argument("--num_heads", type=int, default=4)
    parser.add_argument("--out_dir", type=str, default=None)
    parser.add_argument("--save_every", type=int, default=2000, help="cadence for both validation eval and checkpoint consideration")
    parser.add_argument("--keep_best", type=int, default=3, help="how many step_N/ checkpoints to keep on disk, ranked by validation loss")
    parser.add_argument("--warmup_steps", type=int, default=500)
    parser.add_argument("--num_workers", type=int, default=0)
    parser.add_argument("--bf16", action="store_true", help="mixed precision -- MI300X/A100+ only, skip on older cards")
    parser.add_argument("--data_dir", type=str, default=DEFAULT_REPO_ID,
                         help="HF repo id (default), or a local data/processed/hf_dataset_signs_translit path")
    args = parser.parse_args()

    out_dir = args.out_dir or os.path.join(BASE_DIR, "checkpoints_seq2seq", HF_CONFIG)
    logger = setup_logging(out_dir)

    logger.info(f"Loading '{HF_CONFIG}' config from {args.data_dir} ...")
    if "/" in args.data_dir and not os.path.exists(args.data_dir):
        ds = load_dataset(args.data_dir, HF_CONFIG)
    else:
        ds = load_from_disk(args.data_dir)
    train_pairs = [extract_pair(r) for r in ds["train"]]
    val_pairs = [extract_pair(r) for r in ds["validation"]]
    random.Random(0).shuffle(train_pairs)
    logger.info(f"train pairs: {len(train_pairs)}, validation pairs: {len(val_pairs)}")

    vocab, target_vocab_size = build_vocab(
        target_texts=[t for s, t in train_pairs], source_texts=[s for s, t in train_pairs],
    )
    logger.info(f"vocab size: {len(vocab)} ({target_vocab_size} valid outputs, "
                f"{len(vocab) - target_vocab_size} source-only, suppressed at generation)")

    train_ds = PairDataset(train_pairs, vocab, args.max_len)
    loader = DataLoader(
        train_ds, batch_size=args.batch_size, shuffle=True,
        collate_fn=lambda b: collate(b, vocab[PAD]),
        num_workers=args.num_workers, pin_memory=True, persistent_workers=args.num_workers > 0,
    )
    val_loader = DataLoader(
        PairDataset(val_pairs, vocab, args.max_len), batch_size=args.batch_size, shuffle=False,
        collate_fn=lambda b: collate(b, vocab[PAD]),
    )

    config = T5Config(
        vocab_size=len(vocab), d_model=args.d_model, d_ff=args.d_model * 4,
        num_layers=args.num_layers, num_heads=args.num_heads,
        decoder_start_token_id=vocab[BOS], pad_token_id=vocab[PAD], eos_token_id=vocab[EOS],
    )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = T5ForConditionalGeneration(config).to(device)
    logger.info(f"model params: {sum(p.numel() for p in model.parameters()) / 1e6:.1f}M, device={device}")

    # Kept checkpoints, sorted ascending by validation loss (best first).
    # Each entry is (val_loss, step); the matching directory is out_dir/step_N.
    top_checkpoints: list[tuple[float, int]] = []

    def save_checkpoint(step: int) -> str:
        ckpt_dir = os.path.join(out_dir, f"step_{step}")
        os.makedirs(ckpt_dir, exist_ok=True)
        model.save_pretrained(ckpt_dir)
        json.dump(vocab, open(os.path.join(ckpt_dir, "vocab.json"), "w", encoding="utf-8"), ensure_ascii=False)
        json.dump(vars(args), open(os.path.join(ckpt_dir, "train_args.json"), "w"))
        json.dump({"target_vocab_size": target_vocab_size}, open(os.path.join(ckpt_dir, "vocab_meta.json"), "w"))
        return ckpt_dir

    def consider_checkpoint(step: int, val_loss: float) -> None:
        if len(top_checkpoints) < args.keep_best or val_loss < top_checkpoints[-1][0]:
            save_checkpoint(step)
            top_checkpoints.append((val_loss, step))
            top_checkpoints.sort(key=lambda t: t[0])
            logger.info(f"[checkpoint saved at step {step}] val_loss={val_loss:.3f} -- {out_dir}/step_{step}")
            while len(top_checkpoints) > args.keep_best:
                _, worst_step = top_checkpoints.pop()
                shutil.rmtree(os.path.join(out_dir, f"step_{worst_step}"), ignore_errors=True)
                logger.info(f"[pruned step {worst_step}, no longer in top {args.keep_best}]")
        else:
            logger.info(f"[step {step}] val_loss={val_loss:.3f} -- not competitive, not saved "
                        f"(worst kept: {top_checkpoints[-1][0]:.3f})")

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
                logger.info(f"step {step}/{args.max_steps} loss={out.loss.item():.3f} lr={lr_now:.2e} ({time.time()-t0:.0f}s)")
            if step % args.save_every == 0:
                val_loss = evaluate_loss(model, val_loader, device, amp_dtype)
                logger.info(f"[eval at step {step}] val_loss={val_loss:.3f}")
                consider_checkpoint(step, val_loss)
            if step >= args.max_steps:
                break

    if step % args.save_every != 0:
        val_loss = evaluate_loss(model, val_loader, device, amp_dtype)
        logger.info(f"[final eval at step {step}] val_loss={val_loss:.3f}")
        consider_checkpoint(step, val_loss)

    best_loss, best_step = top_checkpoints[0]
    best_dir = os.path.join(out_dir, "best")
    if os.path.exists(best_dir):
        shutil.rmtree(best_dir)
    shutil.copytree(os.path.join(out_dir, f"step_{best_step}"), best_dir)
    logger.info(f"Done. Best checkpoint: step {best_step} (val_loss={best_loss:.3f}), copied to {best_dir}")
    logger.info(f"Kept checkpoints: {[(s, round(l, 3)) for l, s in top_checkpoints]}")


if __name__ == "__main__":
    main()
