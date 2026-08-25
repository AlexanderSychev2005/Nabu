"""Web demo for the trained Nabu (Akkadian mBERT) checkpoint -- same
stack as kyivan/src/web (FastAPI + a static vanilla-JS page, no separate
frontend build): a single-page tool is plenty for one input box, one image
slot and a handful of result panels, and this keeps the whole project
Python-only rather than adding a Node/Vite toolchain for it.

Serves only checkpoints_final_vision (single model, loaded once at
startup). It was trained on real photos ~16% of the time and an all-zero
placeholder the rest, so it already handles both cases by construction --
there is no separate "text-only" path to fall back to; a request with no
photo just gets that same zero placeholder, exactly matching training. This
also keeps the precomputed corpus embeddings (see compute_embeddings.py,
now likewise pointed at checkpoints_final_vision) in the same embedding
space as a live query -- comparing them would be meaningless if the two
sides came from differently-trained backbones. Everything below is
inference only -- no training, no writes to the corpus.

Run:  python src/web/app.py   (serves http://127.0.0.1:8001)
"""
import base64
import io
import json
import os
import random
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel
from safetensors.torch import load_file
from transformers import AutoTokenizer

sys.path.append(str(Path(__file__).parent.parent.parent))
from src.training.train_mbert import MBertMultiTask, mark_damage_signals, IMG_TRANSFORM_EVAL
from src.data_pipeline.prepare_hf_dataset import clean_transliteration
from src.analysis.interpret import text_gradient_saliency, image_gradcam, document_embedding, nearest_documents

BASE_DIR = Path(__file__).parent.parent.parent
CHECKPOINT = BASE_DIR / "checkpoints_final_vision" / "final_model"
LABEL_CONFIG_PATH = BASE_DIR / "data" / "processed" / "label_configs.json"
EMBEDDINGS_DIR = BASE_DIR / "results_final" / "embeddings"
MODEL_NAME = "bert-base-multilingual-cased"
TASKS = ["period", "genre", "language", "provenience"]
MAX_LENGTH = 96
IMG_SIZE = 224

app = FastAPI(title="Nabu Web")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
tokenizer = None
model = None
label_configs = None
banned_ids = None
doc_embeddings = None
doc_ids = None
doc_meta_by_id = None


class AnalyzeRequest(BaseModel):
    text: str
    image_base64: Optional[str] = None
    mlm_probability: float = 0.15
    seed: int = 0
    temperature: float = 1.0
    restore: bool = True


def load_resources():
    global tokenizer, model, label_configs, banned_ids
    global doc_embeddings, doc_ids, doc_meta_by_id

    with open(LABEL_CONFIG_PATH, encoding="utf-8") as f:
        label_configs = json.load(f)
    num_labels = {t: len(label_configs[t]["labels"]) for t in TASKS}

    print(f"Loading tokenizer from {CHECKPOINT}...")
    tokenizer = AutoTokenizer.from_pretrained(str(CHECKPOINT), use_fast=False)
    banned_ids = set(tokenizer.all_special_ids)

    print("Loading vision checkpoint (single model, used for every request)...")
    model = MBertMultiTask(
        MODEL_NAME, num_period=num_labels["period"], num_genre=num_labels["genre"],
        num_language=num_labels["language"], num_provenience=num_labels["provenience"],
        use_image=True, vision_init="finetune",
    )
    sd = load_file(os.path.join(CHECKPOINT, "model.safetensors"))
    model.load_state_dict(sd)
    model.eval()
    model.to(device)

    emb_path = EMBEDDINGS_DIR / "doc_embeddings.npy"
    meta_path = EMBEDDINGS_DIR / "doc_meta.json"
    if emb_path.exists() and meta_path.exists():
        print(f"Loading precomputed corpus embeddings from {EMBEDDINGS_DIR}...")
        doc_embeddings = np.load(emb_path)
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        doc_ids = [m["tablet_id"] for m in meta]
        doc_meta_by_id = {m["tablet_id"]: m for m in meta}
        print(f"  {len(doc_ids)} documents available for similarity search")
    else:
        print(f"  No precomputed embeddings at {EMBEDDINGS_DIR} -- run compute_embeddings.py first; "
              f"'similar documents' will be empty until then.")
    print("Ready.")


load_resources()


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


def _predict_head(out, task):
    probs = torch.softmax(out[f"{task}_logits"][0], dim=-1).detach()
    conf, idx = probs.max(dim=-1)
    names = label_configs[task]["labels"]
    return {
        "label": names[int(idx.item())],
        "confidence": float(conf.item()),
        "probs": [{"label": n, "prob": float(p)} for n, p in zip(names, probs.tolist())],
    }


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    rng = random.Random(req.seed)
    # clean_transliteration strips [](){}<>| (editorial brackets, ATF sign
    # separators) from the whole string -- protect a literal "[MASK]" (a
    # user can type this directly to pick an exact restoration position)
    # before that runs, or its own brackets get stripped and it silently
    # degrades to a normal "MASK" word instead of the tokenizer's real
    # mask token.
    placeholder = "AKKMASKPLACEHOLDER"
    text = req.text.replace("[MASK]", placeholder)
    text = clean_transliteration(text)
    text = text.replace(placeholder, "[MASK]")
    marked = mark_damage_signals(text)
    full_length = len(tokenizer(marked)["input_ids"])
    enc = tokenizer(marked, truncation=True, max_length=MAX_LENGTH)
    input_ids = enc["input_ids"]
    truncated = full_length > MAX_LENGTH

    # A user can either type literal [MASK] tokens to pick exact positions,
    # or leave plain text and let us auto-mask a random slice (same 15%
    # recipe as training/demo_predictions.py) so the tool still does
    # something useful on a pasted passage with no gap marked. req.restore
    # =False means "just attribute/find parallels for this text as-is" --
    # skip masking (and the whole Restoration section) entirely, for an
    # already-intact text the auto-masker would otherwise damage pointlessly.
    if not req.restore:
        positions = []
    elif tokenizer.mask_token_id in input_ids:
        positions = [i for i, t in enumerate(input_ids) if t == tokenizer.mask_token_id]
    else:
        eligible = [i for i, t in enumerate(input_ids) if t not in banned_ids]
        n_mask = max(1, round(len(eligible) * req.mlm_probability)) if eligible else 0
        positions = sorted(rng.sample(eligible, min(n_mask, len(eligible)))) if eligible else []

    masked_ids = list(input_ids)
    for p in positions:
        masked_ids[p] = tokenizer.mask_token_id

    input_tensor = torch.tensor([masked_ids], device=device)
    attn_tensor = torch.ones_like(input_tensor)

    has_image = bool(req.image_base64)
    if has_image:
        raw = req.image_base64.split(",")[-1]
        img = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
        pixel_values = IMG_TRANSFORM_EVAL(img).unsqueeze(0).to(device)
    else:
        # Same all-zero placeholder used for ~84% of training examples
        # (MBertCollator._zero_image) -- raw zeros, not run through
        # IMG_TRANSFORM_EVAL's normalize, to match training exactly.
        pixel_values = torch.zeros(1, 3, IMG_SIZE, IMG_SIZE, device=device)

    with torch.no_grad():
        out = model(input_ids=input_tensor, attention_mask=attn_tensor, pixel_values=pixel_values)

    # Temperature scales the logits before softmax (higher = flatter/more
    # exploratory top-k, lower = sharper/more conservative) -- same control
    # Aeneas exposes for its own restoration sampling, applied here to our
    # top-5-per-position softmax rather than to a beam search.
    temperature = max(req.temperature, 0.05)
    restorations = []
    for p in positions:
        row_logits = out["logits"][0, p].clone()
        row_logits[list(banned_ids)] = float("-inf")
        topk = torch.topk(torch.softmax(row_logits / temperature, dim=-1), k=5)
        top_k = [
            {"token": tokenizer.convert_ids_to_tokens([i.item()])[0], "prob": float(v.item())}
            for v, i in zip(topk.values, topk.indices)
        ]
        saliency, _ = text_gradient_saliency(
            model, input_tensor, attn_tensor, target="mlm", position=p,
            pixel_values=pixel_values, banned_ids=banned_ids,
        )
        restorations.append({"position": p, "top_k": top_k, "saliency": saliency.tolist()})
    # A single stitched-together reading (each position's own top-1) as one
    # readable line above the per-position breakdown -- the BERT-native
    # analogue of Aeneas's own top beam-search hypothesis, without claiming
    # a true joint-probability argmax across positions (our MLM head scores
    # each masked position independently in one forward pass, not
    # sequentially like their decoder, so there is no real beam to search).
    best_reading = list(tokenizer.convert_ids_to_tokens(masked_ids))
    for r in restorations:
        best_reading[r["position"]] = r["top_k"][0]["token"]
    best_reading = [t for t in best_reading if t not in (tokenizer.cls_token, tokenizer.sep_token)]

    metadata = {t: _predict_head(out, t) for t in TASKS}

    # Text saliency for every metadata head (period/genre/language never
    # see the image regardless, so this doesn't depend on has_image).
    attribution_saliency = {}
    for t in TASKS:
        saliency, _ = text_gradient_saliency(
            model, input_tensor, attn_tensor, target=t,
            pixel_values=pixel_values, banned_ids=banned_ids,
        )
        attribution_saliency[t] = saliency.tolist()

    # Grad-CAM over an all-zero image is a meaningless heatmap (nothing for
    # the convnet to attend to) -- only compute it when a real photo was
    # actually given.
    gradcam = image_gradcam(model, input_tensor, attn_tensor, pixel_values)[0].tolist() if has_image else None

    similar_documents = []
    if doc_embeddings is not None:
        query_emb = document_embedding(model, input_tensor, attn_tensor)
        for tid, score in nearest_documents(query_emb, doc_embeddings, doc_ids, k=20):
            row = doc_meta_by_id.get(tid, {})
            similar_documents.append({
                "tablet_id": tid, "score": score,
                "period": row.get("period"), "genre": row.get("genre"),
                "language": row.get("language"), "provenience": row.get("provenience"),
                "text": row.get("text"), "signs": row.get("signs"),
            })

    return {
        "tokens": tokenizer.convert_ids_to_tokens(masked_ids),
        "truncated": truncated,
        "full_length": full_length,
        "max_length": MAX_LENGTH,
        "masked_positions": positions,
        "restorations": restorations,
        "best_reading": best_reading,
        "metadata": metadata,
        "attribution_saliency": attribution_saliency,
        "gradcam": gradcam,
        "similar_documents": similar_documents,
    }


if __name__ == "__main__":
    # reload=False on purpose: with it on, uvicorn's StatReload spawns a
    # separate watcher process that also imports this module -- the
    # checkpoint (and its ResNet18) end up loaded twice, doubling startup
    # time and memory for a tool with no frontend-only edit loop to speed
    # up. Restart manually after changing app.py.
    uvicorn.run("app:app", host="127.0.0.1", port=8001, reload=False)
