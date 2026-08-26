"""Collect candidate photos for the 33 provenience classes added later in the
corpus cleanup (Hattusa, Mari, Ebla, Susa, Babylon, ...) -- these were
entirely unmapped before, so the existing vision_dataset has zero or
near-zero coverage for them even where CDLI has plenty of photographed
tablets. Caps confirmed with the user: up to ~300/class (matches the
original 12-class convention), floor of 50 available candidates or the class
is skipped (same floor collect_vision_dataset.py already uses).

Two cases per candidate tablet (id_text from cdli_cat.csv, photo_up set,
provenience maps to one of the new classes):
  1. Already has text somewhere in our corpus (documents dataset or the
     interim backfill files) -- only the photo is missing. Just collect the
     photo + manifest entry; no new document row (would duplicate the
     tablet).
  2. Not in the corpus at all -- also try to recover transliterated text
     (CDLI bulk ATF dump, then eBL) via the same atf_to_lines +
     clean_transliteration path as reprocess_bulk_documents.py/
     backfill_text_balance.py. No text found -> skip (a photo with no
     paired text is useless for training, same reasoning backfill_balance.py
     already uses).

Photos: reused from data/raw/cuneiml/images_full if already cached there
(no network call -- checked first), else fetched from CDLI directly (3
workers -- CDLI's photo endpoint chokes on higher concurrency).

Output:
  data/interim/new_provenience_images_documents.jsonl -- new document rows
    (case 2 only), ready for add_cdli_bulk_documents.py's IN_PATHS.
  data/vision_dataset/provenience/<Class>/<numeric_id>.jpg -- both cases.
  data/vision_dataset/manifest.jsonl -- appended for both cases (bbox: the
    CuneiML-suggested box if reused from images_full's own manifest,
    otherwise None) -- still needs review_bboxes_gui.py before it's usable,
    same as every prior backfill.
"""
import csv
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from PIL import Image
from datasets import load_from_disk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.data_pipeline.cuneiform_unicode import atf_to_lines
from src.data_pipeline.prepare_cdli_bulk import split_for
from src.data_pipeline.prepare_hf_dataset import clean_transliteration, map_provenience

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CDLI_CAT_CSV = os.path.join(BASE_DIR, "data", "raw", "cdli_data", "cdli_cat.csv")
ATF_PATH = os.path.join(BASE_DIR, "data", "raw", "cdli_bulk", "cdliatf_unblocked.atf")
EBL_PATH = os.path.join(BASE_DIR, "data", "raw", "cdli_bulk", "ebl_fragments.json")
IMAGES_FULL_DIR = os.path.join(BASE_DIR, "data", "raw", "cuneiml", "images_full")
IMAGES_FULL_MANIFEST = os.path.join(BASE_DIR, "data", "raw", "cuneiml", "images_full_manifest.jsonl")
VISION_BASE = os.path.join(BASE_DIR, "data", "vision_dataset", "provenience")
MANIFEST = os.path.join(BASE_DIR, "data", "vision_dataset", "manifest.jsonl")
OUT_DOCS = os.path.join(BASE_DIR, "data", "interim", "new_provenience_images_documents.jsonl")

CAP = 300
FLOOR = 50
NEW_CLASSES = [
    "Hattusa", "Mari", "Ebla", "Susa", "Babylon", "Nuzi", "Irisagrig", "Persepolis", "Kish", "Larsa",
    "Garšana", "Emar", "Isin", "Ešnunna", "Šaduppum", "Nerebtum", "Šuruppak", "Alalakh", "Kabnak",
    "Kisurra", "Qattara", "Dilbat", "Adab", "Dur-Katlimmu", "Huzirina", "Šubat-Enlil", "Pī-Kasî",
    "Dūr-Abī-ešuḫ", "Tuttul", "Amarna", "Zabalam", "Ašnakkum", "Lagash",
]


def existing_corpus_ids() -> set[str]:
    ids = set()
    ds = load_from_disk(os.path.join(BASE_DIR, "data", "processed", "hf_dataset_documents_with_cdli_bulk"))
    for split in ds:
        ids.update(t for t in ds[split]["tablet_id"] if t)
    return ids


def images_full_index() -> tuple[set[str], dict[str, Optional[list]]]:
    have = set(f[:-4] for f in os.listdir(IMAGES_FULL_DIR) if f.endswith(".jpg"))
    bboxes = {}
    with open(IMAGES_FULL_MANIFEST, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            bboxes[r["id"]] = r.get("bboxes")
    return have, bboxes


def build_atf_body_index() -> dict[str, str]:
    content = open(ATF_PATH, encoding="utf-8", errors="replace").read()
    chunks = re.split(r"(?m)^&(P\d{6})", content)
    return {chunks[i]: (chunks[i + 1] if i + 1 < len(chunks) else "") for i in range(1, len(chunks), 2)}


def build_ebl_atf_index() -> dict[str, str]:
    frags = json.load(open(EBL_PATH, encoding="utf-8"))
    idx = {}
    for f in frags:
        cdli = (f.get("externalNumbers") or {}).get("cdliNumber")
        if cdli:
            idx[cdli] = f.get("atf", "")
    return idx


def parse_tablet_text(body: str) -> str:
    parsed, _misses, _tok = atf_to_lines(body)
    texts = []
    for ln in parsed:
        signs = [s for s in ln["signs"] if s and s != "<S>"]
        if len(signs) < 2:
            continue
        t = clean_transliteration(ln["raw"])
        if t:
            texts.append(t)
    return re.sub(r"\s+", " ", " ".join(texts)).strip()


def fetch_photo(tid: str) -> Optional[bytes]:
    url = f"https://cdli.mpiwg-berlin.mpg.de/dl/photo/{tid}.jpg"
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=60).read()
            Image.open(io.BytesIO(raw)).verify()
            return raw
        except Exception:
            time.sleep(2)
    return None


def main() -> None:
    have = existing_corpus_ids()
    print(f"tablets already in the corpus: {len(have)}")

    cdli_meta_by_id = {}
    by_class = {cls: [] for cls in NEW_CLASSES}
    with open(CDLI_CAT_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idt = str(row.get("id_text", "")).strip()
            if not idt or not row.get("photo_up", "").strip():
                continue
            cls = map_provenience(row.get("provenience", ""))
            if cls in by_class:
                cdli_meta_by_id[idt] = row
                by_class[cls].append(idt)

    img_full_ids, img_full_bboxes = images_full_index()
    print(f"images_full cache: {len(img_full_ids)} files available locally")
    print("building ATF/eBL text indexes...")
    atf_idx = build_atf_body_index()
    ebl_idx = build_ebl_atf_index()

    existing_manifest = set()
    with open(MANIFEST, encoding="utf-8") as f:
        for line in f:
            existing_manifest.add(json.loads(line)["id"])

    new_docs = []
    photo_jobs = []  # (idt, tid, cls, bbox_hint)
    skipped_no_text, skipped_floor = 0, 0

    for cls in NEW_CLASSES:
        pool = by_class[cls]
        if len(pool) < FLOOR:
            skipped_floor += 1
            print(f"{cls}: only {len(pool)} candidates (<{FLOOR} floor), skipping class entirely")
            continue
        n = 0
        for idt in pool:
            if n >= CAP:
                break
            tid = "P" + idt.zfill(6)
            if idt in existing_manifest:
                continue
            if tid in have:
                # text already present -- just needs a photo
                photo_jobs.append((idt, tid, cls, img_full_bboxes.get(idt)))
                n += 1
                continue
            body = atf_idx.get(tid) or ebl_idx.get(tid)
            if body is None:
                continue
            text = parse_tablet_text(body)
            if not text:
                skipped_no_text += 1
                continue
            meta = cdli_meta_by_id.get(idt, {})
            new_docs.append({
                "tablet_id": tid, "text": text,
                "period": meta.get("period", ""), "genre": meta.get("genre", ""),
                "provenience": meta.get("provenience", ""), "language": meta.get("language", ""),
                "split": split_for(tid),
            })
            photo_jobs.append((idt, tid, cls, img_full_bboxes.get(idt)))
            n += 1
        print(f"{cls}: collected {n}/{min(len(pool), CAP)} candidates")

    with open(OUT_DOCS, "w", encoding="utf-8") as out:
        for r in new_docs:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nnew document rows written (text was missing): {len(new_docs)}")
    print(f"skipped (photo_up but no recoverable text): {skipped_no_text}")
    print(f"classes skipped entirely (<{FLOOR} candidates): {skipped_floor}")
    print(f"total photo jobs (existing-text + new-text tablets): {len(photo_jobs)}")

    # Fetch/copy photos: cache hit -> plain file copy, no network; else
    # download (3 workers, gentle).
    from_cache, to_download = [], []
    for idt, tid, cls, bbox in photo_jobs:
        (from_cache if idt in img_full_ids else to_download).append((idt, tid, cls, bbox))
    print(f"from images_full cache (no network): {len(from_cache)}")
    print(f"need live download: {len(to_download)}")

    ok, failed = 0, 0
    with open(MANIFEST, "a", encoding="utf-8") as mf:
        for idt, tid, cls, bbox in from_cache:
            out_dir = os.path.join(VISION_BASE, cls)
            os.makedirs(out_dir, exist_ok=True)
            shutil.copyfile(os.path.join(IMAGES_FULL_DIR, f"{idt}.jpg"), os.path.join(out_dir, f"{idt}.jpg"))
            mf.write(json.dumps({"id": idt, "bbox": bbox}) + "\n")
            ok += 1

        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(fetch_photo, tid): (idt, tid, cls) for idt, tid, cls, _ in to_download}
            done = 0
            for fut in as_completed(futs):
                idt, tid, cls = futs[fut]
                raw = fut.result()
                done += 1
                if raw is None:
                    failed += 1
                    continue
                out_dir = os.path.join(VISION_BASE, cls)
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, f"{idt}.jpg"), "wb") as imf:
                    imf.write(raw)
                mf.write(json.dumps({"id": idt, "bbox": None}) + "\n")
                ok += 1
                if done % 50 == 0:
                    print(f"download progress: {done}/{len(to_download)}", flush=True)

    print(f"\nDONE. photos ok={ok} failed={failed}")


if __name__ == "__main__":
    main()
