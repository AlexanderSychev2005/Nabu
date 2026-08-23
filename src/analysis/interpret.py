"""Post-hoc interpretability for already-trained MBertMultiTask checkpoints --
no retraining, all of this runs on top of checkpoints_final_text/_vision.

Two explanation methods, chosen to match what Aeneas's own authors settled
on (aeneas.pdf, Methods): gradient saliency (Simonyan et al. 2014's
"vanilla gradient" method, ref. 27/83 there) for text, not raw
attention-weight visualization -- the paper explicitly notes attention's
reliability as an explanation is disputed, while their historians found
gradient saliency useful. Grad-CAM is the direct convolutional-network
analogue for the ResNet18 image branch (Aeneas uses ResNet-8 + a similar
image saliency map for the same head).

Also: document_embedding()/nearest_documents(), the same "historically
enriched embedding" Aeneas's contextualization mechanism builds (their
Methods: average of the torso's first output and the mean of the rest) --
translated to our BERT encoder as 0.5*([CLS] + mean of the other real
tokens). Aeneas's own ablation found this simple, un-trained combination
beat trained retrieval alternatives at their data scale; ours is smaller
still, so we inherit the same design rather than building a separate
retrieval model.
"""
import numpy as np
import torch
import torch.nn.functional as F


def text_gradient_saliency(model, input_ids, attention_mask, target, position=None,
                            pixel_values=None, banned_ids=None):
    """Per-token gradient-norm saliency for one scalar target logit.

    target="mlm": saliency for the top-1 restored token at `position`
    (banned_ids excluded from the argmax, same convention as topk_at in
    demo_predictions.py). target in ("period","genre","language",
    "provenience"): saliency for that head's own top-1 predicted class.

    Returns (scores, target_id): scores is a (seq_len,) float32 numpy array
    in [0, 1] (gradient L2-norm per token's input embedding, max-normalized).
    """
    model.zero_grad(set_to_none=True)
    captured = {}

    def hook(_module, _inp, out):
        out.retain_grad()
        captured["emb"] = out

    handle = model.backbone.bert.embeddings.word_embeddings.register_forward_hook(hook)
    try:
        out = model(input_ids=input_ids, attention_mask=attention_mask, pixel_values=pixel_values)
        if target == "mlm":
            assert position is not None, "target='mlm' needs a masked position"
            logits = out["logits"][0, position].clone()
            if banned_ids:
                logits[list(banned_ids)] = float("-inf")
        else:
            logits = out[f"{target}_logits"][0]
        target_id = int(logits.argmax().item())
        logits[target_id].backward()
        grad = captured["emb"].grad
        if grad is None:
            return np.zeros(input_ids.shape[1], dtype=np.float32), target_id
        scores = grad[0].norm(dim=-1).detach().cpu().numpy().astype(np.float32)
    finally:
        handle.remove()
    peak = scores.max()
    if peak > 0:
        scores = scores / peak
    return scores, target_id


def image_gradcam(model, input_ids, attention_mask, pixel_values, target_class=None):
    """Grad-CAM over the ResNet18 provenience branch's last conv block
    (layer4, the standard Grad-CAM choice: the last feature map that still
    has spatial extent). Returns (cam, target_class): cam is a (7, 7)
    float32 array in [0, 1] -- upsample client-side for display."""
    if not model.use_image:
        raise ValueError("Grad-CAM needs the vision (provenience) checkpoint")
    model.zero_grad(set_to_none=True)
    captured = {}

    def hook(_module, _inp, out):
        out.retain_grad()
        captured["feat"] = out

    handle = model.vision_cnn.layer4.register_forward_hook(hook)
    try:
        out = model(input_ids=input_ids, attention_mask=attention_mask, pixel_values=pixel_values)
        logits = out["provenience_logits"][0]
        target_class = int(logits.argmax().item()) if target_class is None else target_class
        logits[target_class].backward()
        feat = captured["feat"][0]
        grad = captured["feat"].grad[0]
        weights = grad.mean(dim=(1, 2))
        cam = F.relu((weights[:, None, None] * feat).sum(dim=0))
    finally:
        handle.remove()
    cam = cam.detach().cpu().numpy().astype(np.float32)
    peak = cam.max()
    if peak > 0:
        cam = cam / peak
    return cam, target_class


def document_embedding(model, input_ids, attention_mask):
    """0.5*([CLS] + mean of the other real tokens) -- see module docstring."""
    with torch.no_grad():
        bert_out = model.backbone.bert(input_ids=input_ids, attention_mask=attention_mask)
        seq = bert_out.last_hidden_state[0]
        mask = attention_mask[0].bool()
        cls = seq[0]
        rest = seq[1:][mask[1:]]
        mean = rest.mean(dim=0) if rest.shape[0] > 0 else cls
        emb = 0.5 * (cls + mean)
    return emb.detach().cpu().numpy().astype(np.float32)


def nearest_documents(query_emb, doc_embeddings, doc_ids, k=5, exclude_id=None):
    """Cosine similarity top-k against a precomputed (N, hidden) matrix
    (see compute_embeddings.py). Returns a list of (tablet_id, score)."""
    q = query_emb / (np.linalg.norm(query_emb) + 1e-8)
    d = doc_embeddings / (np.linalg.norm(doc_embeddings, axis=1, keepdims=True) + 1e-8)
    sims = d @ q
    order = np.argsort(-sims)
    results = []
    for i in order:
        tid = doc_ids[i]
        if tid == exclude_id:
            continue
        results.append((tid, float(sims[i])))
        if len(results) >= k:
            break
    return results
