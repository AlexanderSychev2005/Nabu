"""Top up the existing (well-populated) provenience classes toward a shared
cap instead of leaving them at whatever collect_vision_dataset.py's original
per-source run happened to gather -- confirmed with the user: cap = the
current single largest class's count (Assur, 937 in the finalized
hf_dataset_vision), so no class gets inflated past where the biggest one
already sits. Candidates: CDLI-catalog photo_up tablets whose provenience
maps to a class below the cap, whose tablet_id is already in the text corpus
(hf_dataset_documents_with_cdli_bulk -- a photo with no text is useless), and
not already in data/vision_dataset/manifest.jsonl (covers both reviewed and
pending-review photos, so this never re-fetches a class's already-collected-
but-not-yet-bbox'd backlog).

Only collects the photo + manifest entry -- no new document rows (unlike
backfill_new_provenience_images.py's case-2 path), since every candidate
here already has text in the corpus by construction. Still needs
review_bboxes_gui.py before hf_dataset_vision picks it up.
"""
import csv
import io
import json
import os
import shutil
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from PIL import Image
from datasets import load_from_disk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.data_pipeline.prepare_hf_dataset import map_provenience

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CDLI_CAT_CSV = os.path.join(BASE_DIR, "data", "raw", "cdli_data", "cdli_cat.csv")
IMAGES_FULL_DIR = os.path.join(BASE_DIR, "data", "raw", "cuneiml", "images_full")
IMAGES_FULL_MANIFEST = os.path.join(BASE_DIR, "data", "raw", "cuneiml", "images_full_manifest.jsonl")
VISION_BASE = os.path.join(BASE_DIR, "data", "vision_dataset", "provenience")
MANIFEST = os.path.join(BASE_DIR, "data", "vision_dataset", "manifest.jsonl")

CAP = 937  # confirmed with the user: current largest class (Assur)


def existing_corpus_ids() -> set[str]:
    ds = load_from_disk(os.path.join(BASE_DIR, "data", "processed", "hf_dataset_documents_with_cdli_bulk"))
    ids = set()
    for split in ds:
        ids.update(t for t in ds[split]["tablet_id"] if t)
    return ids


def images_full_index() -> tuple[set[str], dict[str, Optional[list]]]:
    # images_full's raw jpg cache was cleaned up locally since the original
    # backfill_new_provenience_images.py run -- fall back to "nothing
    # cached" (live-download every candidate) rather than erroring out.
    have = set(f[:-4] for f in os.listdir(IMAGES_FULL_DIR) if f.endswith(".jpg")) if os.path.isdir(IMAGES_FULL_DIR) else set()
    bboxes = {}
    with open(IMAGES_FULL_MANIFEST, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            bboxes[r["id"]] = r.get("bboxes")
    return have, bboxes


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

    existing_manifest = set()
    with open(MANIFEST, encoding="utf-8") as f:
        for line in f:
            existing_manifest.add(json.loads(line)["id"])
    print(f"already collected (manifest, reviewed or pending): {len(existing_manifest)}")

    # current per-class count = manifest entries resolved back through the
    # CDLI catalog (covers pending-review photos too, not just the ones
    # finalize_vision_crops.py has already turned into hf_dataset_vision rows).
    current_by_class = Counter()
    pool_by_class = defaultdict(list)
    with open(CDLI_CAT_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idt = str(row.get("id_text", "")).strip()
            if not idt:
                continue
            cls = map_provenience(row.get("provenience", ""))
            if cls == "Unknown":
                continue
            if idt in existing_manifest:
                current_by_class[cls] += 1
                continue
            if not row.get("photo_up", "").strip():
                continue
            tid = "P" + idt.zfill(6) if idt.isdigit() else idt
            if tid not in have:
                continue
            pool_by_class[cls].append(idt)

    img_full_ids, img_full_bboxes = images_full_index()
    print(f"images_full cache: {len(img_full_ids)} files available locally")

    photo_jobs = []  # (idt, cls, bbox_hint)
    for cls, pool in pool_by_class.items():
        need = CAP - current_by_class.get(cls, 0)
        if need <= 0:
            continue
        take = pool[:need]
        for idt in take:
            photo_jobs.append((idt, cls, img_full_bboxes.get(idt)))
        print(f"{cls}: {current_by_class.get(cls, 0)} -> {current_by_class.get(cls, 0) + len(take)} (+{len(take)}, {len(pool)} were available)")

    print(f"\ntotal new photo jobs: {len(photo_jobs)}")

    from_cache, to_download = [], []
    for idt, cls, bbox in photo_jobs:
        (from_cache if idt in img_full_ids else to_download).append((idt, cls, bbox))
    print(f"from images_full cache (no network): {len(from_cache)}")
    print(f"need live download: {len(to_download)}")

    ok, failed = 0, 0
    with open(MANIFEST, "a", encoding="utf-8") as mf:
        for idt, cls, bbox in from_cache:
            out_dir = os.path.join(VISION_BASE, cls)
            os.makedirs(out_dir, exist_ok=True)
            shutil.copyfile(os.path.join(IMAGES_FULL_DIR, f"{idt}.jpg"), os.path.join(out_dir, f"{idt}.jpg"))
            mf.write(json.dumps({"id": idt, "bbox": bbox}) + "\n")
            ok += 1

        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {
                ex.submit(fetch_photo, "P" + idt.zfill(6) if idt.isdigit() else idt): (idt, cls)
                for idt, cls, _ in to_download
            }
            done = 0
            for fut in as_completed(futs):
                idt, cls = futs[fut]
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
                if done % 100 == 0:
                    print(f"download progress: {done}/{len(to_download)}", flush=True)

    print(f"\nDONE. photos ok={ok} failed={failed}")


if __name__ == "__main__":
    main()
