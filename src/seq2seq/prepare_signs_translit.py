"""Build the 'signs_translit' HF config: line-level (signs, transliteration)
pairs for a cuneiform-signs -> transliteration seq2seq experiment.

No new parsing needed -- train/validation splits already exist as
data/processed/hf_dataset (built by prepare_hf_dataset.py from
combined_unique.jsonl), and the held-out test split lives in
data/processed/test.jsonl. Just select the relevant columns from each and
keep the existing tablet-level split assignment (no leakage risk to
re-introduce by re-splitting).

Output: data/processed/hf_dataset_signs_translit (DatasetDict, splits
train/validation/test), columns: signs (list[str]), text (str), tablet_id.
"""
import json
import os

from datasets import Dataset, DatasetDict, load_from_disk

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HF_DATASET_DIR = os.path.join(BASE_DIR, "data", "processed", "hf_dataset")
TEST_PATH = os.path.join(BASE_DIR, "data", "processed", "test.jsonl")
OUT_DIR = os.path.join(BASE_DIR, "data", "processed", "hf_dataset_signs_translit")

KEEP_COLS = ["signs", "text", "tablet_id"]


def _clean(rows: Dataset) -> Dataset:
    rows = rows.select_columns(KEEP_COLS)
    return rows.filter(lambda ex: len(ex["signs"]) >= 2 and ex["text"] and ex["text"].strip())


def main() -> None:
    base = load_from_disk(HF_DATASET_DIR)
    train = _clean(base["train"])
    validation = _clean(base["validation"])

    test_rows = []
    with open(TEST_PATH, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            test_rows.append({"signs": r["signs"], "text": r["text"], "tablet_id": r["tablet_id"]})
    test = _clean(Dataset.from_list(test_rows))

    ds = DatasetDict({"train": train, "validation": validation, "test": test})
    ds.save_to_disk(OUT_DIR)
    print(f"Saved to {OUT_DIR}")
    for split in ds:
        print(f"  {split}: {len(ds[split])}")


if __name__ == "__main__":
    main()
