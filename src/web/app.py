"""Web demo for the trained Nabu (Akkadian mBERT) checkpoints -- same
stack as kyivan/src/web (FastAPI + a static vanilla-JS page, no separate
frontend build): a single-page tool is plenty for one input box, one image
slot and a handful of result panels, and this keeps the whole project
Python-only rather than adding a Node/Vite toolchain for it.

Loads both final checkpoints once at startup (checkpoints_final_text for
restoration/period/genre/language/provenience-from-text-alone, plus
checkpoints_final_vision for provenience-with-image + its Grad-CAM), and
the precomputed corpus embeddings (see compute_embeddings.py) for the
similar-documents lookup. Everything below is inference only -- no
training, no writes to the corpus.

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
TEXT_CHECKPOINT = BASE_DIR / "checkpoints_final_text" / "final_model"
VISION_CHECKPOINT = BASE_DIR / "checkpoints_final_vision" / "final_model"
LABEL_CONFIG_PATH = BASE_DIR / "data" / "processed" / "label_configs.json"
EMBEDDINGS_DIR = BASE_DIR / "results_final" / "embeddings"
MODEL_NAME = "bert-base-multilingual-cased"
TASKS = ["period", "genre", "language", "provenience"]
MAX_LENGTH = 96

app = FastAPI(title="Nabu Web")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
tokenizer = None
text_model = None
vision_model = None
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


def load_resources():
    global tokenizer, text_model, vision_model, label_configs, banned_ids
    global doc_embeddings, doc_ids, doc_meta_by_id

    with open(LABEL_CONFIG_PATH, encoding="utf-8") as f:
        label_configs = json.load(f)
    num_labels = {t: len(label_configs[t]["labels"]) for t in TASKS}

    print(f"Loading tokenizer from {TEXT_CHECKPOINT}...")
    tokenizer = AutoTokenizer.from_pretrained(str(TEXT_CHECKPOINT), use_fast=False)
    banned_ids = set(tokenizer.all_special_ids)

    def _load(checkpoint, use_image, vision_init):
        m = MBertMultiTask(
            MODEL_NAME, num_period=num_labels["period"], num_genre=num_labels["genre"],
            num_language=num_labels["language"], num_provenience=num_labels["provenience"],
            use_image=use_image, vision_init=vision_init,
        )
        sd = load_file(os.path.join(checkpoint, "model.safetensors"))
        m.load_state_dict(sd)
        m.eval()
        return m.to(device)

    print("Loading text-only checkpoint...")
    text_model = _load(TEXT_CHECKPOINT, use_image=False, vision_init="scratch")
    print("Loading vision (provenience) checkpoint...")
    vision_model = _load(VISION_CHECKPOINT, use_image=True, vision_init="finetune")

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
    # something useful on a pasted passage with no gap marked.
    if tokenizer.mask_token_id in input_ids:
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
    pixel_values = None
    if has_image:
        raw = req.image_base64.split(",")[-1]
        img = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
        pixel_values = IMG_TRANSFORM_EVAL(img).unsqueeze(0).to(device)

    with torch.no_grad():
        text_out = text_model(input_ids=input_tensor, attention_mask=attn_tensor)
        # vision_model's own head is only ever shown when a real photo is
        # given (see has_image branch below) -- skip the ResNet18+BERT
        # forward pass entirely otherwise, rather than running it on the
        # zero placeholder just to throw the result away.
        vision_out = (
            vision_model(input_ids=input_tensor, attention_mask=attn_tensor, pixel_values=pixel_values)
            if has_image else None
        )

    restorations = []
    for p in positions:
        row_logits = text_out["logits"][0, p].clone()
        row_logits[list(banned_ids)] = float("-inf")
        topk = torch.topk(torch.softmax(row_logits, dim=-1), k=5)
        top_k = [
            {"token": tokenizer.convert_ids_to_tokens([i.item()])[0], "prob": float(v.item())}
            for v, i in zip(topk.values, topk.indices)
        ]
        saliency, _ = text_gradient_saliency(
            text_model, input_tensor, attn_tensor, target="mlm", position=p, banned_ids=banned_ids,
        )
        restorations.append({"position": p, "top_k": top_k, "saliency": saliency.tolist()})

    metadata = {t: _predict_head(text_out, t) for t in TASKS}

    provenience_vision = None
    provenience_saliency = None
    gradcam = None
    if has_image:
        provenience_vision = _predict_head(vision_out, "provenience")
        saliency, _ = text_gradient_saliency(
            vision_model, input_tensor, attn_tensor, target="provenience",
            pixel_values=pixel_values, banned_ids=banned_ids,
        )
        provenience_saliency = saliency.tolist()
        cam, _ = image_gradcam(vision_model, input_tensor, attn_tensor, pixel_values)
        gradcam = cam.tolist()

    similar_documents = []
    if doc_embeddings is not None:
        query_emb = document_embedding(text_model, input_tensor, attn_tensor)
        for tid, score in nearest_documents(query_emb, doc_embeddings, doc_ids, k=5):
            row = doc_meta_by_id.get(tid, {})
            similar_documents.append({
                "tablet_id": tid, "score": score,
                "period": row.get("period"), "genre": row.get("genre"), "provenience": row.get("provenience"),
            })

    return {
        "tokens": tokenizer.convert_ids_to_tokens(masked_ids),
        "truncated": truncated,
        "full_length": full_length,
        "max_length": MAX_LENGTH,
        "masked_positions": positions,
        "restorations": restorations,
        "metadata": metadata,
        "provenience_vision": provenience_vision,
        "provenience_saliency": provenience_saliency,
        "gradcam": gradcam,
        "similar_documents": similar_documents,
    }


if __name__ == "__main__":
    # reload=False on purpose: with it on, uvicorn's StatReload spawns a
    # separate watcher process that also imports this module -- both
    # checkpoints (and the ResNet18) end up loaded twice, doubling startup
    # time and memory for a tool with no frontend-only edit loop to speed
    # up. Restart manually after changing app.py.
    uvicorn.run("app:app", host="127.0.0.1", port=8001, reload=False)
