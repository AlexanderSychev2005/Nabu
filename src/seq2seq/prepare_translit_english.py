"""Build the 'translit_english' HF config: line-level (transliteration,
English translation) pairs, extracted from '#tr.en:' comment lines already
embedded in the raw ATF of the two bulk sources -- these were previously
only used ad hoc for display in demo_predictions.py, never folded into a
trainable dataset.

Source coverage (checked directly against the raw files): 5,453 CDLI
bulk-ATF tablets (107,237 #tr.en lines) + 1,468 eBL fragments (24,291
#tr.en lines), ~6,913 unique tablets after dedup by tablet_id.

Output: data/processed/hf_dataset_translit_english (DatasetDict, splits
train/validation/test via the same deterministic split_for() hash used
for every other bulk-backfill source), columns: tablet_id, signs
(list[str]), translit (str), translation (str).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from datasets import Dataset, DatasetDict

from src.data_pipeline.cuneiform_unicode import atf_to_lines
from src.data_pipeline.prepare_cdli_bulk import split_for
from src.data_pipeline.prepare_hf_dataset import clean_transliteration
from src.data_pipeline.reprocess_bulk_documents import build_atf_body_index, build_ebl_atf_index
from src.analysis.demo_predictions import parse_translations_by_line

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(BASE_DIR, "data", "processed", "hf_dataset_translit_english")


def extract_pairs(tablet_id: str, body: str) -> list[dict]:
    if not re.search(r"(?m)^#tr", body):
        return []
    parsed, _misses, _tok = atf_to_lines(body)
    translations = parse_translations_by_line(body)
    rows = []
    for ln in parsed:
        key = (ln["face"], ln["num"])
        translation = translations.get(key)
        if not translation:
            continue
        signs = [s for s in ln["signs"] if s and s != "<S>"]
        translit = clean_transliteration(ln["raw"])
        if len(signs) < 2 or not translit:
            continue
        rows.append({
            "tablet_id": tablet_id, "signs": signs,
            "translit": translit, "translation": translation.strip(),
        })
    return rows


def main() -> None:
    atf_idx = build_atf_body_index()
    ebl_idx = build_ebl_atf_index()

    seen_tids = set()
    all_rows = []
    for tid, body in atf_idx.items():
        seen_tids.add(tid)
        all_rows.extend(extract_pairs(tid, body))
    for tid, body in ebl_idx.items():
        if tid in seen_tids:
            continue
        all_rows.extend(extract_pairs(tid, body))

    print(f"tablets with usable #tr.en pairs: {len({r['tablet_id'] for r in all_rows})}")
    print(f"total (transliteration, translation) line pairs: {len(all_rows)}")

    rows_by_split = {"train": [], "validation": [], "test": []}
    for r in all_rows:
        rows_by_split[split_for(r["tablet_id"])].append(r)

    ds = DatasetDict({split: Dataset.from_list(rows) for split, rows in rows_by_split.items()})
    ds.save_to_disk(OUT_DIR)
    print(f"Saved to {OUT_DIR}")
    for split in ds:
        print(f"  {split}: {len(ds[split])}")


if __name__ == "__main__":
    main()
