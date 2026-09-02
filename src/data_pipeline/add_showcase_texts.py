"""Pull well-known named works (Gilgamesh, Enuma Elish, Atrahasis, Hammurabi's
code, Enheduanna's Exaltation of Inanna) into the corpus, forced into the
TEST split regardless of prepare_cdli_bulk.py's normal hash-based
assignment: these exist specifically as recognizable qualitative-demo
examples for the thesis/paper, so they must be held out of training -- a
model that memorized Gilgamesh during training and then "restores" it on a
masked-span demo isn't demonstrating anything.

Same verified path as reprocess_bulk_documents.py: CuneiML's own ATF parser,
dedup against the main corpus's sign-string keys. Photos fetched for the
subset CDLI's catalog flags as having one.

Two source pools: eBL fragments (WORKS, matched by a text needle in the
fragment's own JSON) for the Akkadian/Babylonian works, and CDLI-bulk ATF
directly (CDLI_WORKS, matched by CDLI's own composite-text header comment,
e.g. "= CDLI Literary 000623, ex. 011") for Sumerian literary composites eBL
doesn't carry. Nin-me-sara / The Exaltation of Inanna (composite 000623) is
Enheduanna's self-attributed work -- confirmed directly in the ATF itself,
exemplar P346194 line 5: "en-me-en en-he2-<du7>-an-na" / "I am the en
priestess, I am Enheduana".
"""
import csv
import io
import json
import os
import re
import sys
import time
import urllib.request

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.data_pipeline.collect_vision_dataset import map_provenience
from src.data_pipeline.cuneiform_unicode import atf_to_lines
from src.data_pipeline.prepare_hf_dataset import clean_transliteration

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EBL_PATH = os.path.join(BASE_DIR, "data", "raw", "cdli_bulk", "ebl_fragments.json")
ATF_PATH = os.path.join(BASE_DIR, "data", "raw", "cdli_bulk", "cdliatf_unblocked.atf")
CDLI_CAT_CSV = os.path.join(BASE_DIR, "data", "raw", "cdli_data", "cdli_cat.csv")
COMBINED_PATH = os.path.join(BASE_DIR, "data", "processed", "combined_unique.jsonl")
OUT_DOCS_PATH = os.path.join(BASE_DIR, "data", "interim", "showcase_documents.jsonl")
VISION_BASE = os.path.join(BASE_DIR, "data", "vision_dataset", "provenience")
MANIFEST = os.path.join(BASE_DIR, "data", "vision_dataset", "manifest.jsonl")

WORKS = {
    "Gilgamesh": "gilgame",
    "Enuma Elish": "enuma eli",
    "Atrahasis": "atrahasis",
    "Hammurabi": "hammurabi",
}

# CDLI-bulk composite marker -> work label, for Sumerian literary texts eBL
# doesn't carry. Matched against each tablet's own "&P###### = ..." header
# comment line, not full-text search (composite headers are short and exact).
CDLI_WORKS = {
    "CDLI Literary 000623": "Enheduanna (Exaltation of Inanna)",
    "CDLI Literary 000750": "Enheduanna (Temple Hymns)",
}

# Explicit standalone tablet_ids to pull regardless of composite membership
# -- e.g. a unique dedicatory object with no "ex. NNN" siblings. The Disk of
# Enheduanna (CBS 16665, RIME 2.01.01.16): her own dedicatory inscription
# for a dais in the Inanna-zaza temple at Ur, confirmed via cdli_cat.csv
# (object_remarks: "disc", material: alabaster, provenience: Ur, dated to
# Sargon's reign) -- distinct from the Exaltation of Inanna composite above.
# P217330 is the raw exemplar (ex. 01, no #tr.en lines -- CDLI's own catalog
# marks translation_source "no translation" for it); P461942 is RIME's own
# composite reconstruction of the same inscription, which DOES carry a full
# line-by-line #tr.en translation -- both kept, same pattern as the
# Exaltation of Inanna's exemplars + its own composite P478852 above.
CDLI_SPECIFIC_TIDS = {
    "P217330": "Enheduanna (Disk of Enheduanna)",
    "P461942": "Enheduanna (Disk of Enheduanna)",
    # The Code of Hammurabi stele itself (Louvre Sb 8, RIME 4.03.06.add21) --
    # confirmed via museum_no in cdli_cat.csv (atf_source: Roth, Martha, the
    # standard published edition). The existing WORKS["Hammurabi"] eBL
    # search only ever found smaller clay-tablet copies/excerpts (17 of
    # them, none over ~1.5k chars); this is the full stele text (81k chars
    # raw ATF: prologue + all 282 laws + epilogue), missed because it's a
    # CDLI-bulk entry, not an eBL fragment.
    "P249253": "Hammurabi (Law Code Stele)",
}


def index_cdli_bodies(atf_path: str) -> dict[str, str]:
    content = open(atf_path, encoding="utf-8", errors="replace").read()
    chunks = re.split(r"(?m)^&(P\d{6})", content)
    return {chunks[i]: (chunks[i + 1] if i + 1 < len(chunks) else "") for i in range(1, len(chunks), 2)}


def find_cdli_composite_bodies(atf_path: str, works: dict[str, str]) -> dict[str, tuple[str, str]]:
    """tablet_id -> (raw_atf_body, work_label) for every tablet whose own
    '&P###### = ...' header comment contains one of `works`'s marker keys,
    plus every tablet_id explicitly listed in CDLI_SPECIFIC_TIDS."""
    by_tid = index_cdli_bodies(atf_path)
    found = {}
    for tid, body in by_tid.items():
        header_line = body.split("\n", 1)[0]
        for marker, label in works.items():
            if marker in header_line:
                found[tid] = (body, label)
                break
    for tid, label in CDLI_SPECIFIC_TIDS.items():
        if tid in by_tid:
            found[tid] = (by_tid[tid], label)
    return found


def load_existing_sign_keys() -> set[str]:
    keys = set()
    with open(COMBINED_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = "".join(r.get("signs") or []).strip()
            if k:
                keys.add(k)
    return keys


def main() -> None:
    frags = json.load(open(EBL_PATH, encoding="utf-8"))
    cdli_meta = {}
    with open(CDLI_CAT_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idt = str(row.get("id_text", "")).strip()
            if idt:
                cdli_meta[idt] = row

    existing_keys = load_existing_sign_keys()
    seen_tids = set()

    n_written, n_empty, n_photo_targets = 0, 0, 0
    photo_targets = []
    with open(OUT_DOCS_PATH, "w", encoding="utf-8") as out:
        for work, needle in WORKS.items():
            for f in frags:
                if needle not in json.dumps(f, ensure_ascii=False).lower():
                    continue
                cdli = (f.get("externalNumbers") or {}).get("cdliNumber")
                tid = cdli or f"ebl:{f.get('_id')}"
                if tid in seen_tids:
                    continue
                seen_tids.add(tid)

                parsed, misses, tok = atf_to_lines(f.get("atf", ""))
                tablet_signs, tablet_texts = [], []
                for ln in parsed:
                    signs = [s for s in ln["signs"] if s and s != "<S>"]
                    raw_text = (ln["raw"] or "").strip()
                    if len(signs) < 2 and not raw_text:
                        continue
                    sign_key = "".join(signs) or raw_text
                    if sign_key in existing_keys:
                        continue
                    existing_keys.add(sign_key)
                    tablet_signs.extend(signs)
                    text = clean_transliteration(ln["raw"])
                    if text:
                        tablet_texts.append(text)
                text = re.sub(r"\s+", " ", " ".join(tablet_texts)).strip()
                if not text:
                    n_empty += 1
                    continue

                idt = cdli[1:].lstrip("0") if cdli else None
                meta = (cdli_meta.get(idt) or cdli_meta.get(str(int(idt))) if idt else {}) or {}
                out.write(json.dumps({
                    "tablet_id": tid,
                    "work": work,
                    "text": text,
                    "signs": tablet_signs,
                    "period": meta.get("period", ""),
                    "genre": meta.get("genre", ""),
                    "provenience": meta.get("provenience", ""),
                    "language": meta.get("language", ""),
                    "split": "test",  # forced -- see module docstring
                }, ensure_ascii=False) + "\n")
                n_written += 1

                if cdli and meta.get("photo_up", "").strip():
                    photo_targets.append((idt, cdli, map_provenience(meta.get("provenience", ""))))
                    n_photo_targets += 1

        for tid, (raw_atf, work) in find_cdli_composite_bodies(ATF_PATH, CDLI_WORKS).items():
            if tid in seen_tids:
                continue
            seen_tids.add(tid)

            parsed, misses, tok = atf_to_lines(raw_atf)
            tablet_signs, tablet_texts = [], []
            for ln in parsed:
                signs = [s for s in ln["signs"] if s and s != "<S>"]
                raw_text = (ln["raw"] or "").strip()
                if len(signs) < 2 and not raw_text:
                    continue
                sign_key = "".join(signs) or raw_text
                if sign_key in existing_keys:
                    continue
                existing_keys.add(sign_key)
                tablet_signs.extend(signs)
                text = clean_transliteration(ln["raw"])
                if text:
                    tablet_texts.append(text)
            text = re.sub(r"\s+", " ", " ".join(tablet_texts)).strip()
            if not text:
                n_empty += 1
                continue

            idt = tid[1:].lstrip("0")
            meta = cdli_meta.get(idt) or cdli_meta.get(str(int(idt))) or {}
            out.write(json.dumps({
                "tablet_id": tid,
                "work": work,
                "text": text,
                "signs": tablet_signs,
                "period": meta.get("period", ""),
                "genre": meta.get("genre", ""),
                "provenience": meta.get("provenience", ""),
                "language": meta.get("language", ""),
                "split": "test",  # forced -- see module docstring
            }, ensure_ascii=False) + "\n")
            n_written += 1

            if meta.get("photo_up", "").strip():
                photo_targets.append((idt, tid, map_provenience(meta.get("provenience", ""))))
                n_photo_targets += 1

    print(f"wrote {n_written} showcase documents ({n_empty} empty after parsing/dedup)")
    print(f"photo targets: {n_photo_targets}")

    existing_manifest = set()
    with open(MANIFEST, encoding="utf-8") as f:
        for line in f:
            existing_manifest.add(json.loads(line)["id"])

    ok, failed = 0, []
    with open(MANIFEST, "a", encoding="utf-8") as mf:
        for idt, tid, cls in photo_targets:
            if idt in existing_manifest:
                continue
            out_dir = os.path.join(VISION_BASE, cls if cls != "Unknown" else "Showcase")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"{idt}.jpg")
            url = f"https://cdli.mpiwg-berlin.mpg.de/dl/photo/{tid}.jpg"
            got = False
            for _ in range(3):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    raw = urllib.request.urlopen(req, timeout=60).read()
                    Image.open(io.BytesIO(raw)).verify()
                    with open(out_path, "wb") as fimg:
                        fimg.write(raw)
                    got = True
                    break
                except Exception as e:
                    last = str(e)
                    time.sleep(2)
            if got:
                ok += 1
                mf.write(json.dumps({"id": idt, "bbox": None}) + "\n")
            else:
                failed.append((idt, last))

    print(f"photos: ok={ok} failed={len(failed)}")
    for f in failed:
        print(" fail:", f)


if __name__ == "__main__":
    main()
