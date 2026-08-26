"""Build the 'signs_translit' HF config: line-level (signs, transliteration)
pairs for a cuneiform-signs -> transliteration seq2seq experiment.

Split by the SAME authoritative tablet_id -> split map as documents/vision
(tablet_split_map(), sourced from hf_dataset_documents_with_cdli_bulk) --
NOT combined_unique.jsonl's own pre-override split. A handful of tablet_ids
(an ORACC edition of a showcase work, same tablet_id as the showcase's own
forced-test copy) would otherwise keep their pre-override train/validation
assignment here, contradicting the showcase-holdout guarantee the
documents/vision configs already give: confirmed via a direct check, 7 of
the 83 showcase tablet_ids have such a leaked ORACC edition sitting in
combined_unique.jsonl, 118/45/0 lines across train/validation/test under
the old (unaligned) split.

Output: data/processed/hf_dataset_signs_translit (DatasetDict, splits
train/validation/test), columns: signs (list[str]), text (str), tablet_id.
"""
import json
import os
import sys

from datasets import Dataset, DatasetDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.data_pipeline.build_vision_hf_dataset import tablet_split_map

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMBINED_PATH = os.path.join(BASE_DIR, "data", "processed", "combined_unique.jsonl")
OUT_DIR = os.path.join(BASE_DIR, "data", "processed", "hf_dataset_signs_translit")


def main() -> None:
    split_of = tablet_split_map()

    rows_by_split = {"train": [], "validation": [], "test": []}
    n_unmatched = 0
    with open(COMBINED_PATH, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            signs, text, tid = r.get("signs") or [], (r.get("text") or "").strip(), r.get("tablet_id")
            if len(signs) < 2 or not text:
                continue
            split = split_of.get(tid)
            if split is None:
                n_unmatched += 1
                continue
            rows_by_split[split].append({"signs": signs, "text": text, "tablet_id": tid})

    ds = DatasetDict({split: Dataset.from_list(rows) for split, rows in rows_by_split.items()})
    ds.save_to_disk(OUT_DIR)
    print(f"Saved to {OUT_DIR} ({n_unmatched} rows with no tablet_id in the authoritative split map, skipped)")
    for split in ds:
        print(f"  {split}: {len(ds[split])}")


if __name__ == "__main__":
    main()
