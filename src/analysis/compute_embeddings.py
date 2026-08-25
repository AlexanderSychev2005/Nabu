"""Offline: compute and save one document embedding (interpret.py's
document_embedding -- Aeneas-style 0.5*([CLS] + mean of the rest)) per
tablet, for the web demo's similar-document lookup (src/web/app.py). No
retraining -- reuses the already fine-tuned text-only checkpoint, so this
works for every document regardless of whether it has a photo.

Run once (re-run only if the corpus or checkpoints_final_text changes):

    python src/analysis/compute_embeddings.py

Output: results_final/embeddings/doc_embeddings.npy ((N, hidden) float32)
+ doc_meta.json (one {tablet_id, split, period, genre, language,
provenience} per row, same order) -- src/web/app.py loads both at startup.
"""
import argparse
import json
import os
import sys

import numpy as np
import torch
from safetensors.torch import load_file
from transformers import AutoTokenizer
from datasets import load_dataset
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.training.train_mbert import MBertMultiTask, mark_damage_signals
from src.analysis.interpret import document_embedding

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", default=os.path.join(BASE_DIR, "checkpoints_final_text", "final_model"))
    parser.add_argument("--data_dir", default="AlexSychovUN/Nabu-Dataset")
    parser.add_argument("--hf_config", default="documents")
    parser.add_argument("--model_name", default="bert-base-multilingual-cased")
    parser.add_argument("--label_config", default=os.path.join(BASE_DIR, "data", "processed", "label_configs.json"))
    parser.add_argument("--max_length", type=int, default=96)
    parser.add_argument("--context_char_max", type=int, default=768,
                         help="Matches the document-granularity training window (MBertCollator's own default)")
    parser.add_argument("--out_dir", default=os.path.join(BASE_DIR, "results_final", "embeddings"))
    args = parser.parse_args()

    with open(args.label_config, encoding="utf-8") as f:
        label_configs = json.load(f)
    tasks = ["period", "genre", "language", "provenience"]
    num_labels = {t: len(label_configs[t]["labels"]) for t in tasks}
    label_names = {t: label_configs[t]["labels"] for t in tasks}

    print(f"Loading tokenizer + model from {args.checkpoint}...")
    tokenizer = AutoTokenizer.from_pretrained(args.checkpoint, use_fast=False)
    model = MBertMultiTask(
        args.model_name, num_period=num_labels["period"], num_genre=num_labels["genre"],
        num_language=num_labels["language"], num_provenience=num_labels["provenience"], use_image=False,
    )
    state_dict = load_file(os.path.join(args.checkpoint, "model.safetensors"))
    model.load_state_dict(state_dict)
    model.eval()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)

    print(f"Loading dataset {args.data_dir} ({args.hf_config})...")
    ds = load_dataset(args.data_dir, args.hf_config)

    def label_name(task, idx):
        return label_names[task][idx] if idx is not None and idx != -100 else None

    embeddings, meta = [], []
    for split in ds:
        for row in tqdm(ds[split], desc=split):
            text = mark_damage_signals((row["text"] or "")[:args.context_char_max])
            enc = tokenizer(text, truncation=True, max_length=args.max_length, return_tensors="pt")
            input_ids = enc["input_ids"].to(device)
            attn = enc["attention_mask"].to(device)
            emb = document_embedding(model, input_ids, attn)
            embeddings.append(emb)
            meta.append({
                "tablet_id": row["tablet_id"], "split": split,
                "period": label_name("period", row["period_labels"]),
                "genre": label_name("genre", row["genre_labels"]),
                "language": label_name("language", row["language_labels"]),
                "provenience": label_name("provenience", row["provenience_labels"]),
            })

    os.makedirs(args.out_dir, exist_ok=True)
    arr = np.stack(embeddings).astype(np.float32)
    np.save(os.path.join(args.out_dir, "doc_embeddings.npy"), arr)
    with open(os.path.join(args.out_dir, "doc_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    print(f"Saved {arr.shape[0]} embeddings (dim={arr.shape[1]}) to {args.out_dir}")
