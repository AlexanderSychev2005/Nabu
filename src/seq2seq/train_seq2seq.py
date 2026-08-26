"""Small from-scratch seq2seq (T5 architecture) for either seq2seq side
experiment: signs->transliteration or transliteration->English.

Deliberately NOT the BIO-tagging setup Akkademia (Gordin et al. 2020) uses --
see the seq2seq framing discussion: source and target are plain strings, so
compound-sign / many-to-one alignment never needs explicit handling (the
encoder-decoder attention learns it implicitly).

Source (signs) stays character-level -- every sign is already a single
Unicode codepoint, so there is nothing for a subword tokenizer to merge.
Target (transliteration/English) is byte-level BPE (--bpe_vocab_size),
trained fresh per run on the training split: ATF text is built from
recurring syllables/words ("LUGAL", "-ia", "DINGIR"), so learned subword
units should need fewer generation steps per correct answer than spelling
every case out character by character, unlike the source side. Source and
target still don't share an id space (see build_source_vocab), same
reasoning as before: they're near-disjoint alphabets (measured on
signs_translit: 387 source chars, 81 target chars, only 8 shared), so sharing
one softmax wastes most of the decoder's output distribution on source-only
characters that can never be a valid target token.

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
from collections import defaultdict

import torch
from datasets import load_dataset, load_from_disk
from tokenizers.implementations import ByteLevelBPETokenizer
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


def train_target_tokenizer(target_texts: list[str], vocab_size: int) -> ByteLevelBPETokenizer:
    """Special tokens are added first by the trainer, in the order given,
    so PAD/BOS/EOS/UNK always land at ids 0-3 here -- same convention the
    source vocab below uses, so both sides agree on what those four ids
    mean without needing to coordinate explicitly."""
    tokenizer = ByteLevelBPETokenizer()
    tokenizer.train_from_iterator(target_texts, vocab_size=vocab_size, min_frequency=2, special_tokens=SPECIALS)
    return tokenizer


def build_source_vocab(source_texts: list[str], id_offset: int) -> dict[str, int]:
    """Character-level, ids starting right after the target vocabulary's
    own id range (id_offset = target_tokenizer.get_vocab_size()) -- source
    and target never share an id, so nothing needs de-duplicating between
    the two alphabets the way the old single-shared-vocab version did."""
    chars = sorted(set("".join(source_texts)))
    return {c: id_offset + i for i, c in enumerate(chars)}


def encode_source(text: str, vocab: dict[str, int], eos_id: int, unk_id: int, max_len: int) -> list[int]:
    ids = [vocab.get(c, unk_id) for c in text[: max_len - 1]]
    ids.append(eos_id)
    return ids


def encode_target(text: str, tokenizer: ByteLevelBPETokenizer, eos_id: int, max_len: int) -> list[int]:
    ids = tokenizer.encode(text).ids[: max_len - 1]
    ids.append(eos_id)
    return ids


def build_sign_candidates(pairs: list[tuple[str, str]], target_tokenizer: ByteLevelBPETokenizer) -> dict[str, list[int]]:
    """sign (source character) -> sorted target-token ids ever appearing in
    the same training example -- a per-example (not per-position)
    approximation of Gordin et al. 2020's per-sign candidate dictionary for
    their HMM/MEMM decoding. Their BiLSTM -- the best of their three models
    -- deliberately did NOT use this kind of restriction (its "wildcard"
    ability to propose readings never observed with a given sign was part
    of why it won), so this is applied at generation time as an opt-in
    (--constrain_by_signs in evaluate_seq2seq.py), not baked into training,
    to test empirically whether it helps here rather than assuming the
    HMM/MEMM finding transfers.
    A genuine per-position restriction (matching Gordin exactly) isn't
    available in this seq2seq framing -- there's no guaranteed alignment
    between an input sign and a specific output position (that's the whole
    reason seq2seq was chosen over tagging, see the framing discussion) --
    so this restricts per-example instead: the allowed set for a test input
    is the union of every candidate sign's own observed set, still letting
    the model freely place tokens within that union."""
    candidates: dict[str, set] = defaultdict(set)
    for src, tgt in pairs:
        tgt_ids = set(target_tokenizer.encode(tgt).ids)
        for c in set(src):
            if c == " ":
                continue
            candidates[c] |= tgt_ids
    return {c: sorted(ids) for c, ids in candidates.items()}


class PairDataset(Dataset):
    def __init__(
        self, pairs: list[tuple[str, str]], source_vocab: dict[str, int], target_tokenizer: ByteLevelBPETokenizer,
        eos_id: int, unk_id: int, max_len: int,
    ) -> None:
        self.pairs = pairs
        self.source_vocab = source_vocab
        self.target_tokenizer = target_tokenizer
        self.eos_id = eos_id
        self.unk_id = unk_id
        self.max_len = max_len

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, idx: int) -> tuple[list[int], list[int]]:
        src, tgt = self.pairs[idx]
        return (
            encode_source(src, self.source_vocab, self.eos_id, self.unk_id, self.max_len),
            encode_target(tgt, self.target_tokenizer, self.eos_id, self.max_len),
        )


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
    parser.add_argument("--task", choices=list(TASK_DIRS), required=True)
    parser.add_argument("--max_len", type=int, default=96)
    parser.add_argument("--bpe_vocab_size", type=int, default=600, help="target-side byte-level BPE vocab size")
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
                         help="HF repo id (default), or a local data/processed/hf_dataset_<task> path")
    args = parser.parse_args()

    out_dir = args.out_dir or os.path.join(BASE_DIR, "checkpoints_seq2seq", args.task)
    logger = setup_logging(out_dir)

    extract = TASK_DIRS[args.task]
    logger.info(f"Loading '{args.task}' config from {args.data_dir} ...")
    if "/" in args.data_dir and not os.path.exists(args.data_dir):
        ds = load_dataset(args.data_dir, args.task)
    else:
        ds = load_from_disk(args.data_dir)
    train_pairs = [extract(r) for r in ds["train"]]
    val_pairs = [extract(r) for r in ds["validation"]]
    random.Random(0).shuffle(train_pairs)
    logger.info(f"train pairs: {len(train_pairs)}, validation pairs: {len(val_pairs)}")

    target_texts = [t for s, t in train_pairs]
    source_texts = [s for s, t in train_pairs]
    logger.info(f"training target-side BPE tokenizer (vocab_size<={args.bpe_vocab_size}) ...")
    target_tokenizer = train_target_tokenizer(target_texts, args.bpe_vocab_size)
    target_vocab_size = target_tokenizer.get_vocab_size()
    eos_id, unk_id = target_tokenizer.token_to_id(EOS), target_tokenizer.token_to_id(UNK)
    source_vocab = build_source_vocab(source_texts, id_offset=target_vocab_size)
    total_vocab_size = target_vocab_size + len(source_vocab)
    logger.info(f"vocab: {target_vocab_size} target (BPE) + {len(source_vocab)} source-only chars "
                f"= {total_vocab_size} total (source-only ids suppressed at generation)")

    logger.info("building sign->candidate-target-token co-occurrence table ...")
    sign_candidates = build_sign_candidates(train_pairs, target_tokenizer)

    train_ds = PairDataset(train_pairs, source_vocab, target_tokenizer, eos_id, unk_id, args.max_len)
    loader = DataLoader(
        train_ds, batch_size=args.batch_size, shuffle=True,
        collate_fn=lambda b: collate(b, target_tokenizer.token_to_id(PAD)),
        num_workers=args.num_workers, pin_memory=True, persistent_workers=args.num_workers > 0,
    )
    val_loader = DataLoader(
        PairDataset(val_pairs, source_vocab, target_tokenizer, eos_id, unk_id, args.max_len),
        batch_size=args.batch_size, shuffle=False,
        collate_fn=lambda b: collate(b, target_tokenizer.token_to_id(PAD)),
    )

    config = T5Config(
        vocab_size=total_vocab_size, d_model=args.d_model, d_ff=args.d_model * 4,
        num_layers=args.num_layers, num_heads=args.num_heads,
        decoder_start_token_id=target_tokenizer.token_to_id(BOS),
        pad_token_id=target_tokenizer.token_to_id(PAD), eos_token_id=eos_id,
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
        target_tokenizer.save(os.path.join(ckpt_dir, "target_tokenizer.json"))
        json.dump(source_vocab, open(os.path.join(ckpt_dir, "source_vocab.json"), "w", encoding="utf-8"), ensure_ascii=False)
        json.dump(sign_candidates, open(os.path.join(ckpt_dir, "sign_candidates.json"), "w", encoding="utf-8"), ensure_ascii=False)
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
