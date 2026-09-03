"""Append the new tablets from prepare_cdli_bulk.py's output into the
existing data/processed/hf_dataset_documents DatasetDict, WITHOUT touching
any existing tablet's split assignment (see prepare_cdli_bulk.py's docstring
for why the split field it assigned is used as-is here rather than re-
running prepare_hf_dataset.py's random 90/5/5 split over everything) --
*except* when a backfill tablet_id turns out to already be present in the
base corpus under a different split (mostly ORACC's own edition of a
tablet plus our own showcase/backfill pull of the same physical tablet
under a different sign-string, so the sign-level dedup elsewhere never
catches it). For those, the base
copy is dropped and the backfill's own split wins -- required for
showcase_documents.jsonl specifically, whose whole point is a tablet held
out of training; leaving the base copy in train while the showcase copy
sits in test would defeat that guarantee silently.

Run after prepare_cdli_bulk.py. Saves back to the same dir and (optionally)
pushes the updated 'documents' config to the Hub.
"""
import json
import os
import sys

from datasets import Dataset, DatasetDict, concatenate_datasets, load_from_disk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.data_pipeline.prepare_hf_dataset import (
    GENRE_LABELS, LANGUAGE_LABELS, PERIOD_LABELS, PROVENIENCE_LABELS, label_to_idx,
    map_genre, map_language, map_period, map_provenience,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IN_PATHS = [
    os.path.join(BASE_DIR, "data", "interim", "cdli_bulk_documents.jsonl"),
    os.path.join(BASE_DIR, "data", "interim", "ebl_bulk_documents.jsonl"),
    os.path.join(BASE_DIR, "data", "interim", "balance_documents.jsonl"),
    os.path.join(BASE_DIR, "data", "interim", "text_balance_documents.jsonl"),
    os.path.join(BASE_DIR, "data", "interim", "showcase_documents.jsonl"),
    os.path.join(BASE_DIR, "data", "interim", "new_provenience_images_documents.jsonl"),
]
DOCS_DIR = os.path.join(BASE_DIR, "data", "processed", "hf_dataset_documents")


def split_ids(tablet_id: str) -> list[str]:
    """ORACC's own catalogue occasionally joins several physical P-numbers
    into one tablet_id ("P1; P2; P3", curator-noted joins/exemplar groups)
    -- every exact-string membership check below must compare atomic
    P-numbers, not the joined string, or a backfill source that (correctly)
    treats each P-number as its own document silently duplicates -- and can
    split-leak -- content already covered by the joined base row. Confirmed
    on the rebuilt corpus: 21 cross-split (train/test, train/validation)
    collisions and 78 same-split duplicate rows before this fix."""
    return [p.strip() for p in tablet_id.split(";")] if ";" in tablet_id else [tablet_id]


def main() -> None:
    rows = {"train": [], "validation": [], "test": []}
    seen_tablet_ids = set()
    for in_path in IN_PATHS:
        if not os.path.exists(in_path):
            continue
        with open(in_path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                atoms = split_ids(r["tablet_id"])
                if seen_tablet_ids.intersection(atoms):
                    continue
                seen_tablet_ids.update(atoms)
                rows[r["split"]].append({
                "signs": r.get("signs", []),
                "text": r["text"],
                "tablet_id": r["tablet_id"],
                "period_labels": label_to_idx(map_period(r["period"]), PERIOD_LABELS),
                "genre_labels": label_to_idx(map_genre(r["genre"]), GENRE_LABELS),
                "language_labels": label_to_idx(map_language(r["language"]), LANGUAGE_LABELS),
                "provenience_labels": label_to_idx(map_provenience(r["provenience"]), PROVENIENCE_LABELS),
            })

    ds = load_from_disk(DOCS_DIR)

    # Drop any base-corpus row whose tablet_id a backfill file is about to
    # (re-)introduce, so the backfill's own split always wins and no
    # tablet_id ends up split across two sides at once.
    n_dropped = 0
    for split in ("train", "validation", "test"):
        before = len(ds[split])
        ds[split] = ds[split].filter(lambda ex: not seen_tablet_ids.intersection(split_ids(ex["tablet_id"])))
        n_dropped += before - len(ds[split])
    if n_dropped:
        print(f"Dropped {n_dropped} base-corpus rows whose tablet_id is also in a backfill source "
              f"(backfill's own split assignment wins).")

    for split, new_rows in rows.items():
        if not new_rows:
            continue
        addition = Dataset.from_list(new_rows, features=ds[split].features)
        ds[split] = concatenate_datasets([ds[split], addition])

    # A document with empty transliteration (overwhelmingly ORACC lexical/
    # sign-list projects that have real 'signs' but never had a running
    # transliteration line to begin with) contributes nothing to MLM
    # restoration and gives the metadata heads
    # a classification target with no textual evidence behind it --
    # strictly noise, not a smaller-but-real example.
    n_empty = 0
    for split in ("train", "validation", "test"):
        before = len(ds[split])
        ds[split] = ds[split].filter(lambda ex: ex["text"] and ex["text"].strip())
        n_empty += before - len(ds[split])
    if n_empty:
        print(f"Dropped {n_empty} rows with empty transliteration text.")

    ds.save_to_disk(DOCS_DIR + "_with_cdli_bulk")
    print("Saved to", DOCS_DIR + "_with_cdli_bulk")
    for split in ("train", "validation", "test"):
        print(f"  {split}: {len(ds[split])} ({len(rows[split])} new)")


if __name__ == "__main__":
    main()
