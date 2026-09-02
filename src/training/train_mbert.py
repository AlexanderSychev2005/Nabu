import os
import random
import re
import torch
torch.set_float32_matmul_precision('high')
import torch.nn as nn
import argparse
import json
import logging
from datetime import datetime
from typing import Callable, Optional

from sklearn.metrics import f1_score
import numpy as np
from PIL import Image
from torchvision import models as tv_models
from torchvision import transforms as tv_transforms
from transformers import (
    AutoTokenizer, AutoModelForMaskedLM, BertTokenizer, DataCollatorForLanguageModeling,
    EvalPrediction, PreTrainedTokenizerBase, Trainer, TrainingArguments, TrainerCallback,
    TrainerControl, TrainerState, EarlyStoppingCallback,
)
from datasets import Dataset, load_from_disk, load_dataset
from tokenizers import Tokenizer as RawTokenizer
from tokenizers.models import WordPiece as WordPieceModel
from tokenizers.trainers import WordPieceTrainer
from tokenizers.pre_tokenizers import Whitespace

# Vision branch (--use_image): only provenience gets a picture. A controlled
# 4-way ablation (period/genre/provenience/language, with language as an
# always-image-blind control for run-to-run noise) found provenience is the
# only head with a reproducible, above-noise-floor gain from the image --
# matches Aeneas's own restriction of its vision branch to the geographic-
# attribution head (Assael et al. 2025, Methods p.148: a CNN feature vector
# concatenated with the text embedding before the head). image_heads stays a
# constructor parameter rather than hardcoded, since evaluate_mbert.py and
# demo_predictions.py construct this class directly and should keep working
# if the scope is ever revisited -- see docs/final_results.md for the full
# ablation evidence.
IMG_SIZE = 224  # ResNet18's input size, matches Aeneas's own and finalize_vision_crops.py's stored size
# Rotation/shear/color-jitter ranges approximate Aeneas's own augmentation
# (Methods p.148), with three deliberate deviations: (1) no horizontal flip
# -- a mirrored tablet face has reversed sign order, not a valid input. (2)
# no grayscale conversion -- our images stay RGB because vision_init
# pretrained/finetune loads ImageNet weights whose conv1 filters expect
# 3-channel color. (3) rotation/shear capped at 15/5, not Aeneas's 30/10 --
# their rotation runs on a padded original with room before their own crop
# is chosen; ours runs on an already-tight, human-reviewed 224x224 crop,
# where the wider range clips real inscribed content off-frame.
IMAGENET_MEAN, IMAGENET_STD = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]


def _add_pixel_noise(t: torch.Tensor, max_level: float = 0.05) -> torch.Tensor:
    """Gaussian noise on a [0,1] pixel tensor, matching Aeneas's own
    img_add_random_noise (uniform-random strength per sample)."""
    level = random.uniform(0.0, max_level)
    return (t + torch.randn_like(t) * level).clamp(0.0, 1.0)


IMG_TRANSFORM_TRAIN = tv_transforms.Compose([
    tv_transforms.Resize((IMG_SIZE, IMG_SIZE)),
    tv_transforms.RandomAffine(degrees=15, shear=5),
    tv_transforms.ColorJitter(brightness=0.4, contrast=0.4),
    tv_transforms.RandomApply([tv_transforms.GaussianBlur(5, sigma=(0.1, 2.0))], p=0.5),
    tv_transforms.RandomAdjustSharpness(sharpness_factor=2.0, p=0.5),
    tv_transforms.ToTensor(),
    tv_transforms.Lambda(_add_pixel_noise),
    tv_transforms.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
])
IMG_TRANSFORM_EVAL = tv_transforms.Compose([
    tv_transforms.Resize((IMG_SIZE, IMG_SIZE)),
    tv_transforms.ToTensor(),
    tv_transforms.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
])

# The two real-damage signals that survive into the 'text' column (see
# prepare_hf_dataset.py's clean_transliteration): a standalone 'x' is one
# unclear sign, '...' is a lacuna of unknown length. Neither has its own
# WordPiece token in stock mBERT, so left alone they'd tokenize into
# ordinary maskable subwords -- mBERT would then be trained to "restore"
# positions with no real answer. We reuse two of mBERT's 99 reserved
# [unusedN] vocab slots as dedicated sentinels (same trick Lazar et al. 2021
# use for their own injected tokens); HF's masking collator already excludes
# anything in additional_special_tokens from masking targets.
LONE_X_RE = re.compile(r"\bx\b")
ELLIPSIS_RE = re.compile(r"\.\.\.+")
UNCLEAR_SIGN_TOKEN = "[unused1]"
UNKNOWN_GAP_TOKEN = "[unused2]"


def mark_damage_signals(text: str) -> str:
    text = ELLIPSIS_RE.sub(f" {UNKNOWN_GAP_TOKEN} ", text)
    text = LONE_X_RE.sub(UNCLEAR_SIGN_TOKEN, text)
    return re.sub(r"\s+", " ", text).strip()


def learn_akkadian_tokens(
    texts: list[str], existing_vocab: set[str], n_tokens: int = 97,
    target_vocab_size: int = 8000, min_frequency: int = 10,
) -> list[str]:
    """Reproduce Lazar et al. 2021's free-token trick: they assign mBERT's
    99 unused vocab slots by "optimizing for maximum likelihood by the
    WordPiece tokenization algorithm" but never published the exact list --
    relearn it here by training a fresh WordPiece vocabulary on our own
    corpus and keeping the highest-frequency pieces mBERT doesn't already
    have. Without this, Akkadian-specific sign sequences get chopped into
    excessive fragments by mBERT's stock (mostly-modern-language) vocab."""
    tok = RawTokenizer(WordPieceModel(unk_token="[UNK]"))
    tok.pre_tokenizer = Whitespace()
    trainer = WordPieceTrainer(
        vocab_size=target_vocab_size, min_frequency=min_frequency,
        special_tokens=["[UNK]"], continuing_subword_prefix="##",
    )
    tok.train_from_iterator(texts, trainer=trainer)
    learned = sorted(tok.get_vocab().items(), key=lambda kv: kv[1])  # id order ~ frequency rank
    candidates = [t for t, _ in learned if t not in existing_vocab and t != "[UNK]" and t.replace("##", "")]
    return candidates[:n_tokens]


def inject_akkadian_tokens(
    tokenizer: PreTrainedTokenizerBase, new_tokens: list[str], first_free_slot: int = 3,
) -> tuple[BertTokenizer, int]:
    """Rename mBERT's unused [unusedN] vocab slots (N >= first_free_slot,
    since 1-2 are already claimed by the damage sentinels) to the learned
    Akkadian tokens, keeping the same embedding row id -- no vocab growth,
    no resize_token_embeddings() needed.

    BertTokenizer's `.vocab` is a detached snapshot dict in this
    transformers version -- mutating it in place never reaches the actual
    Rust WordPiece tokenizer used for encoding. The only reliable way to
    rename a slot is to rebuild the tokenizer from a modified vocab dict, so
    this returns a *new* tokenizer instance rather than mutating in place."""
    vocab = dict(tokenizer.get_vocab())
    n_injected = 0
    for i, new_tok in enumerate(new_tokens):
        slot = f"[unused{i + first_free_slot}]"
        if slot not in vocab:
            break
        vocab[new_tok] = vocab.pop(slot)
        n_injected += 1

    new_tokenizer = BertTokenizer(
        vocab=vocab, do_lower_case=tokenizer.do_lower_case,
        unk_token=tokenizer.unk_token, sep_token=tokenizer.sep_token,
        pad_token=tokenizer.pad_token, cls_token=tokenizer.cls_token,
        mask_token=tokenizer.mask_token,
        tokenize_chinese_chars=tokenizer.tokenize_chinese_chars,
        strip_accents=tokenizer.strip_accents,
    )
    return new_tokenizer, n_injected


class TiedWeightSafeTrainer(Trainer):
    """BertForMaskedLM ties cls.predictions.decoder.weight/bias to
    bert.embeddings.word_embeddings.weight (standard tied-embeddings MLM
    head), so state_dict() has two keys aliasing the same tensor storage. A
    real PreTrainedModel's save_pretrained() de-duplicates this
    automatically; MBertMultiTask is a plain nn.Module wrapper, so Trainer
    routes through the generic save path, which calls
    safetensors.torch.save_file() directly on the raw state_dict with no
    format fallback. Cloning each tensor gives every key its own storage,
    satisfying safetensors' shared-memory check -- the live model's actual
    weight tying during training is untouched, this only affects what gets
    written to disk."""

    def _save(self, output_dir: Optional[str] = None, state_dict: Optional[dict] = None) -> None:
        if state_dict is None:
            state_dict = self.model.state_dict()
        state_dict = {k: v.clone() for k, v in state_dict.items()}
        super()._save(output_dir, state_dict=state_dict)

    # --use_image trains with augmented crops (see IMG_TRANSFORM_TRAIN) but
    # eval should stay deterministic so checkpoint comparisons (early
    # stopping, with-vs-without-image deltas) aren't noisy from random
    # rotation/color jitter. HF's Trainer only takes one data_collator
    # constructor arg, so swap it in for the duration of eval only.
    def __init__(self, *args, eval_data_collator: Optional["MBertCollator"] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.eval_data_collator = eval_data_collator

    def get_eval_dataloader(self, eval_dataset=None):
        if self.eval_data_collator is None:
            return super().get_eval_dataloader(eval_dataset)
        original = self.data_collator
        self.data_collator = self.eval_data_collator
        try:
            return super().get_eval_dataloader(eval_dataset)
        finally:
            self.data_collator = original


class LogToFileCallback(TrainerCallback):
    # report_to="none" leaves Trainer's default PrinterCallback printing
    # step/eval metrics straight to stdout (bypasses the `logging` module),
    # so the FileHandler on the module logger never sees them.
    def on_log(
        self, args: TrainingArguments, state: TrainerState, control: TrainerControl,
        logs: Optional[dict] = None, **kwargs,
    ) -> None:
        if logs is not None:
            logging.getLogger(__name__).info(f"step {state.global_step}: {logs}")


class MBertMultiTask(nn.Module):
    """mBERT baseline: joint MLM + 4 metadata classification heads (period/
    genre/language/provenience), following Lazar et al. 2021's finding that
    a pretrained multilingual model finetuned on Akkadian outperforms a
    from-scratch model at this data scale."""

    def __init__(
        self, model_name: str, num_period: int, num_genre: int, num_language: int, num_provenience: int,
        meta_weight: float = 1.0, use_image: bool = False, vision_init: str = "scratch",
        img_feat_dim: int = 128, image_heads: tuple[str, ...] = ("provenience",),
    ) -> None:
        super().__init__()
        self.backbone = AutoModelForMaskedLM.from_pretrained(model_name)
        hidden_size = self.backbone.config.hidden_size
        self.use_image = use_image
        self.meta_weight = meta_weight
        self.image_heads = set(image_heads) if use_image else set()

        vision_head_in = hidden_size + img_feat_dim if use_image else hidden_size

        def head_in_size(name: str) -> int:
            return vision_head_in if name in self.image_heads else hidden_size

        self.period_head = nn.Linear(head_in_size("period"), num_period)
        self.genre_head = nn.Linear(head_in_size("genre"), num_genre)
        self.language_head = nn.Linear(hidden_size, num_language)  # never sees the image
        self.provenience_head = nn.Linear(head_in_size("provenience"), num_provenience)

        if use_image:
            # scratch: random init, fully trainable ResNet18 -- underperforms
            # at this image-count scale (not enough data/steps for an 11M-
            # param CNN to learn useful filters from random noise).
            # pretrained: frozen ImageNet ResNet18, only vision_proj trains
            # -- a linear probe on fixed general-purpose features.
            # finetune: same ImageNet init as pretrained, but not frozen --
            # lets the CNN adapt its features to tablet photos specifically;
            # this is the mode the shipped checkpoints actually use.
            weights = tv_models.ResNet18_Weights.IMAGENET1K_V1 if vision_init in ("pretrained", "finetune") else None
            resnet = tv_models.resnet18(weights=weights)
            if vision_init == "pretrained":
                for p in resnet.parameters():
                    p.requires_grad = False
            resnet.fc = nn.Identity()
            self.vision_cnn = resnet
            self.vision_proj = nn.Linear(512, img_feat_dim)
            # Matches Aeneas's own x_img_norm (LayerNorm right after the
            # vision embedding, before concatenation with text) -- cls_embed
            # comes out of BERT already well-scaled by its internal
            # LayerNorms; a raw linear projection of ResNet features has no
            # such guarantee, especially early in training.
            self.vision_norm = nn.LayerNorm(img_feat_dim)

    def forward(
        self, input_ids: torch.Tensor, attention_mask: Optional[torch.Tensor] = None,
        pixel_values: Optional[torch.Tensor] = None, labels: Optional[torch.Tensor] = None,
        period_labels: Optional[torch.Tensor] = None, genre_labels: Optional[torch.Tensor] = None,
        language_labels: Optional[torch.Tensor] = None, provenience_labels: Optional[torch.Tensor] = None,
    ) -> dict[str, Optional[torch.Tensor]]:
        bert_out = self.backbone.bert(input_ids=input_ids, attention_mask=attention_mask)
        seq = bert_out.last_hidden_state
        mlm_logits = self.backbone.cls(seq)

        cls_embed = seq[:, 0, :]
        if self.use_image:
            img_feat = self.vision_norm(self.vision_proj(self.vision_cnn(pixel_values)))
            head_in = torch.cat([cls_embed, img_feat], dim=-1)
        else:
            head_in = cls_embed
        period_logits = self.period_head(head_in if "period" in self.image_heads else cls_embed)
        genre_logits = self.genre_head(head_in if "genre" in self.image_heads else cls_embed)
        language_logits = self.language_head(cls_embed)
        provenience_logits = self.provenience_head(head_in if "provenience" in self.image_heads else cls_embed)

        loss = None
        if any(l is not None for l in [labels, period_labels, genre_labels, language_labels, provenience_labels]):
            loss_mlm_fct = nn.CrossEntropyLoss(ignore_index=-100, label_smoothing=0.05)
            loss_meta_fct = nn.CrossEntropyLoss(ignore_index=-100, label_smoothing=0.1)
            loss = 0.0

            # MLM=3.0, meta_weight defaults to 1.0 -- roughly matches
            # Aeneas's own multi-task weighting (restoration=3, region=2,
            # date=1.25). Configurable via --meta_weight for further tuning.
            if labels is not None and (labels != -100).any():
                loss += 3.0 * loss_mlm_fct(mlm_logits.view(-1, mlm_logits.size(-1)), labels.view(-1))

            for logits, lbl in [(period_logits, period_labels), (genre_logits, genre_labels),
                                 (language_logits, language_labels), (provenience_logits, provenience_labels)]:
                if lbl is not None and (lbl != -100).any():
                    loss += self.meta_weight * loss_meta_fct(logits, lbl)

        return {
            "loss": loss,
            "logits": mlm_logits,
            "period_logits": period_logits,
            "genre_logits": genre_logits,
            "language_logits": language_logits,
            "provenience_logits": provenience_logits,
        }


def build_tablet_image_index(crops_dir: str, reviewed_only: bool = True) -> dict[str, Image.Image]:
    """tablet_id (CDLI "P######" form) -> PIL.Image, opened eagerly. Only
    used when --use_image; ids outside this index (the overwhelming
    majority of the corpus) fall back to an all-zero placeholder in the
    collator, same as Aeneas's own training (every example carries an image
    slot, real or not)."""
    manifest_path = os.path.join(crops_dir, "crops_manifest.jsonl")
    index: dict[str, Image.Image] = {}
    if not os.path.exists(manifest_path):
        return index
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if reviewed_only and not row.get("reviewed"):
                continue
            raw_id = str(row["id"]).strip()
            tablet_id = "P" + raw_id.zfill(6) if raw_id.isdigit() else raw_id
            path = os.path.join(crops_dir, f"{row['id']}.jpg")
            if os.path.exists(path):
                try:
                    index[tablet_id] = Image.open(path).convert("RGB")
                except Exception:
                    pass
    return index


def build_tablet_image_index_from_hf(repo_id: str) -> dict[str, Image.Image]:
    """Same tablet_id -> PIL.Image mapping as build_tablet_image_index, but
    pulled straight from the "vision" HF config instead of a local crops
    folder -- so a training box only needs `git pull` + this script, no
    scp'ing image folders around."""
    vision_ds = load_dataset(repo_id, "vision")
    index: dict[str, Image.Image] = {}
    for split in vision_ds:
        for row in vision_ds[split]:
            index[row["tablet_id"]] = row["image"].convert("RGB")
    return index


def mark_one_line_per_tablet(dataset: Dataset) -> Dataset:
    """Adds an "image_tablet_id" column: equal to "tablet_id" for exactly
    one (the first-encountered) line of each tablet, "" for every other
    line of that same tablet. TRAIN-only fix for a real skew: without this,
    a tablet's photo is shown to the model once per line it has (up to 407,
    avg 13), and that count varies systematically by class -- capping it to
    one real showing per tablet per epoch removes that skew without
    touching line-level MLM."""
    # Plain sequential pass (not .map(num_proc>1)) -- "first encountered"
    # must follow actual row order, which parallel/batched execution
    # wouldn't guarantee.
    seen = set()
    marked = []
    for tid in dataset["tablet_id"]:
        if not tid or tid in seen:
            marked.append("")
        else:
            seen.add(tid)
            marked.append(tid)
    return dataset.add_column("image_tablet_id", marked)


class MBertCollator:
    """Standard 15% MLM masking (HF's own collator) plus the 4 metadata
    labels carried through. HF's collator already excludes anything in
    tokenizer.additional_special_tokens from masking targets, so
    registering UNCLEAR_SIGN_TOKEN/UNKNOWN_GAP_TOKEN as special tokens is
    enough to keep them out of the mask targets here too.

    image_index (tablet_id -> PIL.Image) is None when --use_image is off,
    in which case no pixel_values key is produced at all. img_transform
    selects train (augmented) vs eval (deterministic) processing.

    context_char_max (set only for the document-granularity dataset, where
    some documents exceed mBERT's 512-token position-embedding ceiling):
    instead of always keeping a long document's first max_length tokens,
    follow Aeneas's own approach and sample a random character window per
    example, so the model sees every *part* of a long document across
    training. Requires "text" to still be a raw (untokenized) column --
    tokenization happens here, per batch. training=True gives a random
    start position AND random window length each call; training=False
    (eval) takes a fixed window from the start so repeated evaluate() calls
    stay reproducible."""

    def __init__(
        self, tokenizer: PreTrainedTokenizerBase, mlm_probability: float = 0.15,
        image_index: Optional[dict[str, Image.Image]] = None, img_transform=IMG_TRANSFORM_EVAL,
        context_char_min: Optional[int] = None, context_char_max: Optional[int] = None,
        max_length: int = 96, training: bool = False,
    ) -> None:
        self.tokenizer = tokenizer
        self.mlm_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=True, mlm_probability=mlm_probability)
        self.image_index = image_index
        self.img_transform = img_transform
        self.context_char_min = context_char_min
        self.context_char_max = context_char_max
        self.max_length = max_length
        self.training = training
        self._zero_image = torch.zeros(3, IMG_SIZE, IMG_SIZE)

    def _load_image(self, tablet_id: Optional[str]) -> torch.Tensor:
        img = self.image_index.get(tablet_id) if tablet_id else None
        if img is None:
            return self._zero_image
        try:
            return self.img_transform(img)
        except Exception:
            return self._zero_image

    def _window(self, text: str) -> str:
        if not self.context_char_max or len(text) <= self.context_char_max:
            return text
        if self.training:
            length = random.randint(min(self.context_char_min, len(text)), self.context_char_max)
            start = random.randint(0, len(text) - length)
            return text[start:start + length]
        return text[:self.context_char_max]

    def _tokenize(self, ex: dict) -> dict:
        text = mark_damage_signals(self._window(ex["text"]))
        enc = self.tokenizer(text, truncation=True, max_length=self.max_length)
        # MBertMultiTask.forward() only accepts input_ids/attention_mask --
        # drop token_type_ids (BertTokenizer returns it by default).
        return {"input_ids": enc["input_ids"], "attention_mask": enc["attention_mask"]}

    def __call__(self, examples: list[dict]) -> dict[str, torch.Tensor]:
        if self.context_char_max is not None:
            pre = [self._tokenize(ex) for ex in examples]
        else:
            pre = [{"input_ids": ex["input_ids"], "attention_mask": ex["attention_mask"]} for ex in examples]
        batch = self.mlm_collator(pre)
        for task in ["period", "genre", "language", "provenience"]:
            batch[f"{task}_labels"] = torch.tensor([ex[f"{task}_labels"] for ex in examples], dtype=torch.long)
        if self.image_index is not None:
            # "image_tablet_id" (see mark_one_line_per_tablet) is blank for
            # every line of a tablet except one, on the TRAIN split only --
            # falls back to plain "tablet_id" if that column wasn't added
            # (eval: every line of an image-bearing tablet gets its real
            # photo, since eval isn't fighting a training-time bias).
            batch["pixel_values"] = torch.stack([
                self._load_image(ex["image_tablet_id"] if "image_tablet_id" in ex else ex.get("tablet_id"))
                for ex in examples
            ])
        return batch


def make_preprocess_logits_for_metrics(banned_ids: set[int]) -> Callable:
    """banned_ids: PAD/UNK/CLS/SEP/MASK plus the two injected damage
    sentinels -- none of these is ever a valid restoration answer."""
    banned = torch.tensor(sorted(banned_ids), dtype=torch.long)

    def preprocess_logits_for_metrics(logits: tuple[torch.Tensor, ...], labels: tuple[torch.Tensor, ...]):
        # MBertMultiTask has no HF PretrainedConfig, so Trainer's
        # prediction_step converts the output dict into a plain positional
        # tuple (dropping "loss") before calling this function -- must
        # index by position, matching MBertMultiTask.forward's dict
        # insertion order: logits, period_logits, genre_logits,
        # language_logits, provenience_logits.
        mlm_logits = logits[0].clone()
        mlm_logits[..., banned.to(mlm_logits.device)] = float("-inf")
        mlm_top5 = torch.topk(mlm_logits, k=5, dim=-1).indices

        # Full-vocab rank of the true token, computed here (not in
        # compute_metrics) so we never have to hold the full (B, S, V)
        # logits in the accumulated eval predictions: rank = 1 + count of
        # logits that beat the target's own logit. labels[0] is the
        # primary MLM "labels" tensor; -100 (unmasked) positions get
        # clamped to a dummy valid index and filtered out downstream.
        mlm_labels = labels[0]
        safe_labels = mlm_labels.clamp(min=0)
        target_logits = mlm_logits.gather(-1, safe_labels.unsqueeze(-1))
        rank = (mlm_logits > target_logits).sum(dim=-1) + 1

        meta_preds = [torch.argmax(logits[i], dim=-1) for i in range(1, 5)]
        return (mlm_top5, rank, *meta_preds)

    return preprocess_logits_for_metrics


def compute_metrics(eval_pred: EvalPrediction) -> dict[str, float]:
    preds = eval_pred.predictions
    label_ids = eval_pred.label_ids
    metrics = {}

    task_names = ["period", "genre", "language", "provenience"]
    for i, task in enumerate(task_names):
        task_preds = preds[i + 2].reshape(-1)
        task_labels = label_ids[i + 1].reshape(-1)
        mask = task_labels != -100
        if not mask.any():
            metrics[f"{task}_acc"] = 0.0
            metrics[f"{task}_macro_f1"] = 0.0
            continue
        task_preds, task_labels = task_preds[mask], task_labels[mask]
        metrics[f"{task}_acc"] = float((task_preds == task_labels).mean())
        metrics[f"{task}_macro_f1"] = float(f1_score(task_labels, task_preds, average="macro", zero_division=0))

    mlm_preds = preds[0].reshape(-1, 5)
    mlm_rank = preds[1].reshape(-1)
    mlm_labels = label_ids[0].reshape(-1)
    mlm_mask = mlm_labels != -100
    if mlm_mask.any():
        masked_preds = mlm_preds[mlm_mask]
        masked_labels = mlm_labels[mlm_mask]
        metrics["mlm_acc"] = float((masked_preds[:, 0] == masked_labels).mean())
        metrics["mlm_top3_acc"] = float(np.any(masked_preds[:, :3] == masked_labels[:, None], axis=1).mean())
        metrics["mlm_top5_acc"] = float(np.any(masked_preds == masked_labels[:, None], axis=1).mean())
        # Same metric Lazar et al. 2021 report (their Table 2, MRR + Hit@5).
        metrics["mlm_mrr"] = float((1.0 / mlm_rank[mlm_mask]).mean())
    else:
        metrics["mlm_acc"] = metrics["mlm_top3_acc"] = metrics["mlm_top5_acc"] = metrics["mlm_mrr"] = 0.0

    return metrics


def train() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_dir", type=str, default=r"C:\Programming\akkadian\data\processed\hf_dataset")
    parser.add_argument("--label_config", type=str, default=None, help="Path to label_configs.json (sizes the metadata heads); auto-resolved from --data_dir if omitted")
    parser.add_argument("--model_name", type=str, default="bert-base-multilingual-cased")
    parser.add_argument("--save_dir", type=str, default="checkpoints_mbert")
    parser.add_argument("--batch_size", type=int, default=64, help="Starting point for a 16GB GPU -- adjust based on actual VRAM usage")
    parser.add_argument("--grad_accum", type=int, default=4)
    parser.add_argument("--num_workers", type=int, default=4)
    parser.add_argument("--lr", type=float, default=2e-5, help="Standard BERT finetuning LR")
    parser.add_argument("--meta_weight", type=float, default=1.0, help="Loss weight for each metadata head; MLM restoration is fixed at 3.0")
    parser.add_argument("--epochs", type=int, default=20, help="Lazar et al. 2021 finetune mBERT for 20 epochs on Akkadian")
    parser.add_argument("--eval_steps", type=int, default=500)
    parser.add_argument("--early_stopping_patience", type=int, default=4)
    # Real token-length distribution (mBERT's own WordPiece tokenizer over
    # combined_unique.jsonl): median=18, p99=72, p99.9=120 -- 96 covers
    # 99.7% of examples at little more than half the attention FLOPs of 128.
    parser.add_argument("--max_length", type=int, default=512)
    parser.add_argument("--precision", type=str, choices=["fp32", "fp16", "bf16"], default="fp16", help="Mixed precision mode -- fp16 for T4/Colab, bf16 for Ampere+ (A100/newer)")
    parser.add_argument("--resume_from_checkpoint", type=str, default=None, help="Path to a specific checkpoint, or 'auto' to resume from the latest one in --save_dir")
    parser.add_argument("--use_image", action="store_true", help="Add the vision branch (Aeneas-style concat), scoped to provenience_head only, ImageNet-finetune init")
    parser.add_argument("--crops_dir", type=str, default=r"C:\Programming\akkadian\data\vision_dataset_final", help="Dir with <tablet id>.jpg crops + crops_manifest.jsonl (see finalize_vision_crops.py); ignored if --images_from_hf")
    parser.add_argument("--include_unreviewed", action="store_true", help="Also use tablets whose bbox was never manually reviewed -- off by default")
    parser.add_argument("--images_from_hf", action="store_true", help="Load the vision config straight from --data_dir's HF repo instead of a local --crops_dir")
    parser.add_argument("--hf_config", type=str, default="default", help="Which HF dataset config to load when --data_dir is a Hub repo id (e.g. 'documents' for the tablet-granularity dataset)")
    parser.add_argument("--context_char_min", type=int, default=32, help="Aeneas-style random text windowing: minimum window length in characters. Only used if --context_char_max is set")
    parser.add_argument("--context_char_max", type=int, default=None, help="Enables random-window sampling of 'text' at collate time instead of always keeping the first --max_length tokens. None (default) = pre-tokenize once and truncate from the start")
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    os.makedirs(args.save_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(os.path.join(args.save_dir, f"training_log_{timestamp}.txt")), logging.StreamHandler()],
    )
    logger = logging.getLogger(__name__)
    logger.info(f"Using device: {device}")

    # use_fast=False: inject_akkadian_tokens() rebuilds a BertTokenizer from
    # a modified vocab dict, which only the slow tokenizer class supports as
    # a constructor argument. WordPiece tokenization is identical either way.
    tokenizer = AutoTokenizer.from_pretrained(args.model_name, use_fast=False)

    logger.info(f"Loading datasets from {args.data_dir} (config={args.hf_config})...")
    if "/" in args.data_dir and not os.path.exists(args.data_dir):
        hf_ds = load_dataset(args.data_dir, args.hf_config)
    else:
        hf_ds = load_from_disk(args.data_dir)

    logger.info("Learning Akkadian-specific WordPiece tokens for mBERT's free vocab slots...")
    akkadian_tokens = learn_akkadian_tokens(hf_ds["train"]["text"], set(tokenizer.get_vocab().keys()), n_tokens=97)
    tokenizer, n_injected = inject_akkadian_tokens(tokenizer, akkadian_tokens, first_free_slot=3)
    logger.info(f"Injected {n_injected} Akkadian tokens into mBERT's unused[3..{2 + n_injected}] slots")

    # add_special_tokens on a token string already in the vocab only
    # registers it as special (stops it being split, stops it being
    # masked) -- it does not grow the vocab or add a new embedding row.
    tokenizer.add_special_tokens({"additional_special_tokens": [UNCLEAR_SIGN_TOKEN, UNKNOWN_GAP_TOKEN]})

    # --context_char_max skips pre-tokenization entirely: MBertCollator
    # tokenizes from raw "text" per batch instead, so it can draw a fresh
    # random character window each time (see MBertCollator._window).
    if args.context_char_max is not None:
        hf_ds = hf_ds.remove_columns(["signs"])
    else:
        def tokenize_fn(examples):
            marked = [mark_damage_signals(t) for t in examples["text"]]
            return tokenizer(marked, truncation=True, max_length=args.max_length)
        hf_ds = hf_ds.map(tokenize_fn, batched=True, remove_columns=["text", "signs"], num_proc=max(1, os.cpu_count() - 1))
    train_dataset = hf_ds["train"]
    val_dataset = hf_ds["validation"]
    logger.info(f"Loaded {len(train_dataset)} training samples.")

    image_index = None
    if args.use_image:
        if args.images_from_hf:
            image_index = build_tablet_image_index_from_hf(args.data_dir)
            logger.info(f"Vision branch on (finetune, provenience_head only): {len(image_index)} tablets have a real photo "
                        f"(loaded from {args.data_dir}'s 'vision' config); everything else gets an all-zero placeholder image")
        else:
            image_index = build_tablet_image_index(args.crops_dir, reviewed_only=not args.include_unreviewed)
            logger.info(f"Vision branch on (finetune, provenience_head only): {len(image_index)} tablets have a real photo "
                        f"({'including' if args.include_unreviewed else 'excluding'} unreviewed bboxes); "
                        f"everything else gets an all-zero placeholder image")
        # TRAIN only: cap each tablet to one real image showing per epoch
        # (see mark_one_line_per_tablet) -- eval keeps every line's real
        # image. A no-op at document granularity (already one row/tablet).
        train_dataset = mark_one_line_per_tablet(train_dataset)
        n_marked = sum(1 for t in train_dataset["image_tablet_id"] if t)
        logger.info(f"mark_one_line_per_tablet: {n_marked} rows (of {len(train_dataset)}) keep their real "
                    f"image slot for training, one per tablet")
    collator = MBertCollator(tokenizer, image_index=image_index, img_transform=IMG_TRANSFORM_TRAIN,
                              context_char_min=args.context_char_min, context_char_max=args.context_char_max,
                              max_length=args.max_length, training=True)
    eval_collator = MBertCollator(tokenizer, image_index=image_index, img_transform=IMG_TRANSFORM_EVAL,
                                   context_char_min=args.context_char_min, context_char_max=args.context_char_max,
                                   max_length=args.max_length, training=False) \
        if (args.use_image or args.context_char_max is not None) else None

    if args.label_config:
        label_config_path = args.label_config
    elif os.path.exists(args.data_dir):
        label_config_path = r"C:\Programming\akkadian\data\processed\label_configs.json"
    else:
        from huggingface_hub import hf_hub_download
        label_config_path = hf_hub_download(repo_id=args.data_dir, filename="configs/label_configs.json", repo_type="dataset")
    with open(label_config_path, "r", encoding="utf-8") as f:
        label_configs = json.load(f)
    tasks = ["period", "genre", "language", "provenience"]
    num_labels = {task: len(label_configs[task]["labels"]) for task in tasks}
    logger.info(f"Metadata head sizes from {label_config_path}: {num_labels}")

    logger.info(f"Initializing {args.model_name}...")
    model = MBertMultiTask(
        args.model_name, num_period=num_labels["period"], num_genre=num_labels["genre"],
        num_language=num_labels["language"], num_provenience=num_labels["provenience"],
        meta_weight=args.meta_weight, use_image=args.use_image, vision_init="finetune",
    )

    # TrainingArguments(logging_dir=...) is deprecated in favor of this env
    # var (transformers >= 5.x) -- must be set before the TensorBoardCallback
    # reads it in on_train_begin.
    os.environ["TENSORBOARD_LOGGING_DIR"] = os.path.join(args.save_dir, "runs")

    training_args = TrainingArguments(
        output_dir=args.save_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        eval_strategy="steps",
        eval_steps=args.eval_steps,
        save_strategy="steps",
        save_steps=args.eval_steps,
        save_total_limit=3,
        logging_steps=100,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_steps=500,
        weight_decay=0.01,
        fp16=(args.precision == "fp16"),
        bf16=(args.precision == "bf16"),
        dataloader_num_workers=args.num_workers,
        report_to=["tensorboard"],
        label_names=["labels", "period_labels", "genre_labels", "language_labels", "provenience_labels"],
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        # Trainer's default (True) strips any dataset column not in
        # MBertMultiTask.forward()'s signature before the collator ever
        # sees a batch -- breaks both --context_char_max (collator
        # tokenizes from "text" itself) and --use_image (collator looks up
        # "tablet_id"/"image_tablet_id"), neither of which is a forward()
        # parameter.
        remove_unused_columns=False,
    )

    trainer = TiedWeightSafeTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        data_collator=collator,
        eval_data_collator=eval_collator,
        compute_metrics=compute_metrics,
        preprocess_logits_for_metrics=make_preprocess_logits_for_metrics(tokenizer.all_special_ids),
        callbacks=[EarlyStoppingCallback(early_stopping_patience=args.early_stopping_patience), LogToFileCallback()],
    )

    logger.info("Starting training with Hugging Face Trainer...")
    resume = True if args.resume_from_checkpoint == "auto" else args.resume_from_checkpoint
    trainer.train(resume_from_checkpoint=resume)

    logger.info("Training complete. Saving final state and metrics...")
    trainer.save_model(os.path.join(args.save_dir, "final_model"))
    tokenizer.save_pretrained(os.path.join(args.save_dir, "final_model"))

    with open(os.path.join(args.save_dir, f"training_history_{timestamp}.json"), "w", encoding="utf-8") as f:
        json.dump(trainer.state.log_history, f, indent=2, ensure_ascii=False)
    logger.info("History saved.")


if __name__ == "__main__":
    train()
