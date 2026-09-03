"""Redo the three bulk-backfill sources (CDLI-bulk-ATF Uruk/Nimrud rescue,
eBL rescue, balance backfill) through CuneiML's own ATF parser
(src/data_pipeline/cuneiform_unicode.py) instead of the ad-hoc
line-stripping used when they were first built -- gets a real 'signs'
(Unicode cuneiform) column matching the rest of the corpus, not an empty
one, and reports the sign-resolution miss rate for QA.

Also re-checks every new line against the MAIN corpus's own dedup key
(joined sign-string, same as prepare_hf_dataset.py's load_and_deduplicate_v2)
-- read-only against combined_unique.jsonl, does NOT touch or rebuild it,
so the existing train/val/test split stays untouched (see prepare_cdli_bulk.py
and add_cdli_bulk_documents.py's docstrings for why that split must not be
re-shuffled this late).

Output: data/interim/{cdli_bulk,ebl_bulk,balance}_documents.jsonl,
overwritten in place with a 'signs' field added (previously absent) and any
now-duplicate tablets dropped.
"""
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.data_pipeline.cuneiform_unicode import atf_to_lines
from src.data_pipeline.prepare_cdli_bulk import split_for
from src.data_pipeline.prepare_hf_dataset import clean_transliteration

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ATF_PATH = os.path.join(BASE_DIR, "data", "raw", "cdli_bulk", "cdliatf_unblocked.atf")
EBL_PATH = os.path.join(BASE_DIR, "data", "raw", "cdli_bulk", "ebl_fragments.json")
CDLI_CAT_CSV = os.path.join(BASE_DIR, "data", "raw", "cdli_data", "cdli_cat.csv")
COMBINED_PATH = os.path.join(BASE_DIR, "data", "processed", "combined_unique.jsonl")

SOURCES = ["cdli_bulk_documents.jsonl", "ebl_bulk_documents.jsonl", "balance_documents.jsonl"]


def load_existing_sign_keys() -> set[tuple[str, str]]:
    """(tablet_id, line-sign-key) pairs, not bare sign-keys -- a bare key
    collides constantly across unrelated tablets (a single common word or
    formulaic name recurs verbatim throughout the corpus by design), which
    used to silently drop that line from whichever tablet processed it
    later, even when it was that tablet's own distinctive content. See
    add_showcase_texts.py's load_existing_sign_keys for the confirmed case
    (the Enheduanna disc, P217330, losing its own name-line this way)."""
    keys = set()
    with open(COMBINED_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            signs = r.get("signs") or []
            k = "".join(signs).strip()
            if k:
                keys.add((r.get("tablet_id") or "", k))
    return keys


def build_atf_body_index() -> dict[str, str]:
    content = open(ATF_PATH, encoding="utf-8", errors="replace").read()
    chunks = re.split(r"(?m)^&(P\d{6})", content)
    idx = {}
    for i in range(1, len(chunks), 2):
        idx[chunks[i]] = chunks[i + 1] if i + 1 < len(chunks) else ""
    return idx


def build_ebl_atf_index() -> dict[str, str]:
    frags = json.load(open(EBL_PATH, encoding="utf-8"))
    idx = {}
    for f in frags:
        cdli = (f.get("externalNumbers") or {}).get("cdliNumber")
        if cdli:
            idx[cdli] = f.get("atf", "")
    return idx


def main() -> None:
    atf_idx = build_atf_body_index()
    ebl_idx = build_ebl_atf_index()
    existing_keys = load_existing_sign_keys()
    print(f"existing corpus sign-keys (for dedup check): {len(existing_keys)}")

    cdli_meta_by_id = {}
    with open(CDLI_CAT_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idt = str(row.get("id_text", "")).strip()
            if idt:
                cdli_meta_by_id[idt] = row

    total_miss, total_tok = 0, 0
    grand_written, grand_dropped_empty, grand_dropped_dupe = 0, 0, 0

    for fname in SOURCES:
        path = os.path.join(BASE_DIR, "data", "interim", fname)
        if not os.path.exists(path):
            continue
        old_rows = [json.loads(l) for l in open(path, encoding="utf-8")]
        new_rows = []
        n_written, n_empty, n_dupe = 0, 0, 0

        for old in old_rows:
            tid = old["tablet_id"]
            body = atf_idx.get(tid, ebl_idx.get(tid))
            if body is None:
                continue
            parsed, misses, tok = atf_to_lines(body)
            total_miss += sum(misses.values())
            total_tok += tok

            tablet_signs, tablet_texts = [], []
            for ln in parsed:
                signs = [s for s in ln["signs"] if s and s != "<S>"]
                raw_text = (ln["raw"] or "").strip()
                # len(signs) < 2 alone used to drop the line even when it
                # has real transliteration text -- see prepare_oracc.py's
                # docstring for the confirmed case (a normalized-reading
                # ORACC edition with no sign-level data at all for some
                # lemmas). CDLI-bulk/eBL ATF is syllable-based so this
                # rarely fires here, but the same guard is cheap to add.
                if len(signs) < 2 and not raw_text:
                    continue
                sign_key = "".join(signs) or raw_text
                key = (tid, sign_key)
                if key in existing_keys:
                    n_dupe += 1
                    continue
                existing_keys.add(key)  # dedup within this run too
                tablet_signs.extend(signs)
                text = clean_transliteration(ln["raw"])
                if text:
                    tablet_texts.append(text)

            text = re.sub(r"\s+", " ", " ".join(tablet_texts)).strip()
            if not text:
                n_empty += 1
                continue

            new_rows.append({
                "tablet_id": tid,
                "text": text,
                "signs": tablet_signs,
                "period": old.get("period", ""),
                "genre": old.get("genre", ""),
                "provenience": old.get("provenience", ""),
                "language": old.get("language", ""),
                # Preserve the row's existing split rather than recomputing
                # it -- normally a no-op (split_for(tid) is deterministic,
                # same input same output), except for the rare row whose
                # split was manually forced outside that scheme (P387407,
                # a showcase-adjacent example forced into "test"), where
                # recomputing would silently move it back to whatever the
                # hash says instead (confirmed: it lands in "train").
                "split": old.get("split") or split_for(tid),
            })
            n_written += 1

        with open(path, "w", encoding="utf-8") as out:
            for r in new_rows:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")

        print(f"{fname}: {len(old_rows)} -> {n_written} written "
              f"({n_empty} now empty after real parsing, {n_dupe} were sign-level dupes of the main corpus)")
        grand_written += n_written
        grand_dropped_empty += n_empty
        grand_dropped_dupe += n_dupe

    print(f"\nTOTAL written: {grand_written}, dropped empty: {grand_dropped_empty}, dropped dupe: {grand_dropped_dupe}")
    print(f"Sign-resolution miss rate: {total_miss}/{total_tok} = {100*total_miss/max(total_tok,1):.1f}%")


if __name__ == "__main__":
    main()
