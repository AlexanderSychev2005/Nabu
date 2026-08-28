"""Per-line (cuneiform | transliteration | translation) tables for the web
demo's "Similar documents" modal -- a real line-by-line parse, not this
project's own flattened whole-document 'text'/'signs'/'translation' columns
(which lose line boundaries in the corpus merge, same reason
demo_predictions.py builds its own line table for the showcase writeup).

Two source paths, matching backfill_translations.py's own source priority:

1. CDLI-bulk ATF / eBL fragment 'atf' -- reuses cuneiform_unicode.py's own
   atf_to_lines() (the same function that builds this project's 'signs'
   column) plus a per-line '#tr.en:' pairing, so a line gets all three
   columns when the tablet has a translation, two (cuneiform + translit)
   when it doesn't.
2. ORACC's cached HTML pages (see backfill_translations.py's docstring for
   why this cache exists at all) -- per-line transliteration ('td.tlit')
   and translation ('td.t1.xtr'), but no cuneiform: ORACC's own rendering
   doesn't include Unicode cuneiform glyphs, only transliteration, so these
   rows are translit + translation only (two columns, never three).

Output: results_final/embeddings/doc_lines.json,
{tablet_id: {"source": "CDLI"/"eBL"/"ORACC (<project>)", "source_url": str,
"lines": [{"face", "num", "signs", "translit", "translation"}, ...]}},
one entry per tablet_id actually present in hf_dataset_documents_with_cdli_bulk
(no point building this for the raw sources' full ~135k/25k coverage when
only ~57k of them are tablets we actually show). 'source'/'source_url'
identify which edition the table was actually built from -- CDLI-bulk, eBL,
or a specific ORACC project -- since it isn't always the same edition the
tablet_id's own top-level link (app.py's sourceUrl(), by tablet_id prefix)
points to: a P-number-keyed tablet whose translation only exists on ORACC
gets its line table from ORACC even though the page header still links to
its CDLI record (both links are shown; they can legitimately point to two
different editions of the same physical tablet).

Run:  python -m src.analysis.build_line_tables
"""
import json
import multiprocessing as mp
import os
import re
import sys
import zipfile

from bs4 import BeautifulSoup
from datasets import load_from_disk
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.data_pipeline.cuneiform_unicode import atf_to_lines, _FACE_KEYS
from src.data_pipeline.backfill_translations import (
    ORACC_HTML_ZIP, EBL_PATH, CDLI_ATF_PATH, clean_translation, _ORACC_P_NUMBER_RE, _ORACC_LABEL_RE,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCS_DIR = os.path.join(BASE_DIR, "data", "processed", "hf_dataset_documents_with_cdli_bulk")
OUT_PATH = os.path.join(BASE_DIR, "results_final", "embeddings", "doc_lines.json")

_P_HEADER_RE = re.compile(r"^&P(\d+)")
_TR_EN_RE = re.compile(r"^#tr\.en:\s*(.*)$")


def _atf_body_index(path: str) -> dict[str, str]:
    """{"P######": full raw ATF body} -- same tablet-boundary scan as
    backfill_translations.py's extract_cdli_bulk_translations(), just
    keeping the whole body instead of only '#tr.en:' lines."""
    bodies: dict[str, list[str]] = {}
    current_id, current_lines = None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _P_HEADER_RE.match(line)
            if m:
                if current_id and current_lines:
                    bodies[current_id] = current_lines
                current_id, current_lines = f"P{m.group(1)}", []
            elif current_lines is not None:
                current_lines.append(line)
        if current_id and current_lines:
            bodies[current_id] = current_lines
    return {k: "".join(v) for k, v in bodies.items()}


def _line_table_from_atf(raw_atf: str) -> list[dict]:
    parsed, _misses, _tok = atf_to_lines(raw_atf)
    # (face, num, occurrence#) -> translation, from '#tr.en:' immediately
    # following the content line it annotates. The occurrence# (which Nth
    # time this exact (face, num) label has appeared, 0-indexed) matters
    # because ATF line numbering restarts within each '@column' -- a marker
    # atf_to_lines() itself doesn't track (its 'face' stays e.g. "obverse"
    # across both columns), so "obverse, line 1" legitimately appears twice
    # for a genuinely two-column tablet, not just once. Using an ordinal
    # instead of trying to parse '@column N' explicitly works because both
    # this scan and the loop below walk the exact same numbered-line
    # sequence in the exact same order, so the Nth repeat here always lines
    # up with the Nth repeat there, regardless of what caused the repeat.
    translations: dict[tuple[str, str, int], str] = {}
    seen1: dict[tuple[str, str], int] = {}
    curr_face, pending = "default", None
    for line in raw_atf.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("@"):
            key = line[1:].strip().strip("?")
            if key in _FACE_KEYS:
                curr_face = key
            pending = None
            continue
        m = _TR_EN_RE.match(line)
        if m:
            if pending and m.group(1).strip():
                translations[pending] = m.group(1).strip()
            continue
        if line.startswith(("#", ">>", "$", "&")):
            continue
        lm = re.match(r"(\S+)\.\s+(.*)", line)
        if lm:
            base = (curr_face, lm.group(1))
            idx = seen1.get(base, 0)
            seen1[base] = idx + 1
            pending = (*base, idx)
        else:
            pending = None

    # A repeated (face, num) is either a genuine multi-piece join (CDLI's
    # ATF doesn't mark the second physical fragment with its own face/
    # object tag atf_to_lines() recognizes -- near-identical content, one
    # entry just richer/more complete) or a second column/section that
    # happens to restart numbering at the same values (materially
    # different content -- a real, confirmed case: K.2433 has an
    # '@column 1' and '@column 2', both starting their own line "1", with
    # totally different text). Similar text collapses to the richer copy
    # (old behavior); different text keeps both rather than silently
    # discarding one -- the old version just picked "whichever has more
    # signs" regardless of whether the two repeats were even the same line.
    by_key: dict[tuple, dict] = {}
    order: list[tuple] = []
    seen2: dict[tuple[str, str], int] = {}
    for ln in parsed:
        signs = [s for s in ln["signs"] if s and s != "<S>"]
        if not signs and not ln["raw"].strip():
            continue
        base = (ln["face"], ln["num"])
        idx = seen2.get(base, 0)
        seen2[base] = idx + 1
        row = {
            "face": ln["face"], "num": ln["num"], "signs": " ".join(signs),
            "translit": ln["raw"], "translation": translations.get((*base, idx)),
        }
        prev = by_key.get(base)
        if prev is None:
            by_key[base], order = row, order + [base]
        elif row["translit"] in prev["translit"] or prev["translit"] in row["translit"]:
            if len(row["signs"]) > len(prev["signs"]):
                by_key[base] = row
        else:
            dup_key = (*base, idx)
            row["num"] = f"{ln['num']} (cont.)"
            by_key[dup_key], order = row, order + [dup_key]
    return [by_key[k] for k in order]


def _oracc_line_table_from_html(html: bytes) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("tr.l"):
        # The lnum cell repeats its own label in two sibling spans
        # ('xlabel' + 'lnum', same text, presumably for two different CSS
        # display modes) -- take just one, or get_text() on the whole cell
        # doubles it.
        lnum_span = row.select_one("td.lnum span.lnum")
        tlit_cell = row.select_one("td.tlit")
        if not tlit_cell:
            continue
        translit = _clean_oracc_cell(tlit_cell.get_text(" ", strip=True))
        if not translit:
            continue
        tr_cell = row.select_one("td.t1.xtr p.tr, td.xtr p.tr")
        translation = None
        if tr_cell:
            t = _ORACC_LABEL_RE.sub("", tr_cell.get_text(" ", strip=True))
            t = _clean_oracc_cell(t)
            translation = clean_translation(t) if t and t != "(break)" else None
        lnum = _clean_oracc_cell(lnum_span.get_text(" ", strip=True)) if lnum_span else str(len(out) + 1)
        out.append({"face": "default", "num": lnum, "signs": None, "translit": translit, "translation": translation})
    return out


def _clean_oracc_cell(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,;:!?)\]⸣])", r"\1", text)
    text = re.sub(r"([(\[⸢])\s+", r"\1", text)
    # ORACC's per-syllable spans put a space around every '-' too (it's
    # rendering one <span> per syllable, not per word) -- collapse to match
    # the plain "a-na" style the rest of this project's transliteration
    # already uses (cuneiform_unicode.py's own ATF conversion never adds
    # these spaces).
    text = re.sub(r"\s*-\s*", "-", text)
    return text.strip()


def _init_html_worker(zip_path: str) -> None:
    global _HTML_ZIP
    _HTML_ZIP = zipfile.ZipFile(zip_path)


def _process_oracc_name(name: str) -> tuple[str, list[dict]]:
    return name, _oracc_line_table_from_html(_HTML_ZIP.read(name))


def main() -> None:
    ds = load_from_disk(DOCS_DIR)
    tablet_ids = set()
    for split in ds.keys():
        tablet_ids.update(ds[split]["tablet_id"])
    print(f"{len(tablet_ids)} tablet_ids in corpus")

    print("Indexing CDLI-bulk ATF bodies...")
    cdli_bodies = _atf_body_index(CDLI_ATF_PATH)
    print(f"  {len(cdli_bodies)} tablets")

    print("Indexing eBL fragment atf fields...")
    with open(EBL_PATH, encoding="utf-8") as f:
        ebl_frags = json.load(f)
    ebl_bodies: dict[str, str] = {}
    ebl_ids: dict[str, str] = {}  # tid -> eBL's own museum-number id, for a direct fragmentarium link even when tid is a cross-referenced P-number
    for frag in ebl_frags:
        atf = frag.get("atf") or ""
        if not atf.strip():
            continue
        cdli = (frag.get("externalNumbers") or {}).get("cdliNumber")
        tid = cdli or f"ebl:{frag.get('_id')}"
        ebl_bodies[tid] = atf
        ebl_ids[tid] = frag.get("_id")
    print(f"  {len(ebl_bodies)} fragments")

    atf_tables: dict[str, list[dict]] = {}
    atf_source: dict[str, tuple[str, str]] = {}  # tid -> (label, url)
    for tid in tqdm(tablet_ids, desc="CDLI/eBL line tables"):
        if tid in ebl_bodies:
            raw, src = ebl_bodies[tid], ("eBL", f"https://www.ebl.lmu.de/fragmentarium/{ebl_ids[tid]}")
        elif tid in cdli_bodies:
            raw, src = cdli_bodies[tid], ("CDLI", f"https://cdli.earth/artifacts/{tid[1:]}" if tid.startswith("P") else None)
        else:
            continue
        lines = _line_table_from_atf(raw)
        if lines:
            atf_tables[tid], atf_source[tid] = lines, src
    n_atf_with_translation = sum(1 for ls in atf_tables.values() if any(l["translation"] for l in ls))
    print(f"  {len(atf_tables)} tablets got a CDLI/eBL line table ({n_atf_with_translation} with >=1 translated line)")

    # ORACC HTML path -- checked for every tablet_id that either has no ATF
    # line table at all, or has one with zero translated lines (like
    # P394430: CDLI-bulk has its signs/transliteration but its translation
    # actually lives on ORACC's own asbp/ninmed edition, not in CDLI-bulk's
    # '#tr.en:' lines -- the two sources are complementary, not one
    # superseding the other, so a translation-empty ATF table doesn't mean
    # this tablet truly has no translation anywhere).
    needs_oracc = {tid for tid in tablet_ids if not any(l["translation"] for l in atf_tables.get(tid, []))}
    html_names = [n for n in zipfile.ZipFile(ORACC_HTML_ZIP).namelist() if n.endswith(".html")]
    # A P-number filename isn't unique across projects -- e.g. Amarna
    # letters are independently hosted under both 'aemw/amarna' and
    # 'contrib/amarna' with the same P271129.html name, one of the two
    # carrying a real translation and the other empty. Keep every candidate
    # per key instead of letting whichever happens to sort last silently
    # win (which was this function's actual bug before this fix -- it lost
    # a real translation to an empty duplicate on ~465 colliding keys).
    names_by_key: dict[str, list[str]] = {}
    for name in html_names:
        project_path, textid = name.rsplit("/", 1)
        textid = textid[:-len(".html")]
        key = textid if _ORACC_P_NUMBER_RE.match(textid) else f"oracc:{project_path}:{textid}"
        names_by_key.setdefault(key, []).append(name)
    todo_names = [name for k in needs_oracc for name in names_by_key.get(k, [])]
    print(f"Parsing {len(todo_names)} ORACC HTML pages...")
    lines_by_name: dict[str, list[dict]] = {}
    with mp.Pool(processes=10, initializer=_init_html_worker, initargs=(ORACC_HTML_ZIP,)) as pool:
        for name, lines in tqdm(pool.imap_unordered(_process_oracc_name, todo_names, chunksize=25), total=len(todo_names)):
            if lines:
                lines_by_name[name] = lines

    def _table_rank(name: str) -> tuple[int, int]:
        lines = lines_by_name[name]
        return (sum(1 for l in lines if l["translation"]), len(lines))

    oracc_tables: dict[str, list[dict]] = {}
    oracc_source: dict[str, tuple[str, str]] = {}
    for key in needs_oracc:
        candidates = [n for n in names_by_key.get(key, []) if n in lines_by_name]
        if not candidates:
            continue
        best_name = max(candidates, key=_table_rank)
        oracc_tables[key] = lines_by_name[best_name]
        project_path, textid = best_name.rsplit("/", 1)
        oracc_source[key] = (f"ORACC ({project_path})", f"http://oracc.org/{project_path}/{textid[:-len('.html')]}")

    # Prefer whichever table actually carries a translation; an ATF table
    # with real cuneiform signs wins on a tie (both or neither translated).
    result: dict[str, dict] = {}
    n_atf = n_oracc = 0
    for tid in tablet_ids:
        atf_lines = atf_tables.get(tid)
        oracc_lines = oracc_tables.get(tid)
        atf_has_tr = bool(atf_lines) and any(l["translation"] for l in atf_lines)
        oracc_has_tr = bool(oracc_lines) and any(l["translation"] for l in oracc_lines)
        if atf_has_tr or (atf_lines and not oracc_has_tr):
            lines, (label, url), n_atf = atf_lines, atf_source[tid], n_atf + 1
        elif oracc_lines:
            lines, (label, url), n_oracc = oracc_lines, oracc_source[tid], n_oracc + 1
        else:
            continue
        result[tid] = {"source": label, "source_url": url, "lines": lines}
    print(f"  {n_atf} tablets used the CDLI/eBL table, {n_oracc} used the ORACC-HTML table")

    print(f"Total: {len(result)}/{len(tablet_ids)} tablets with a line table ({100*len(result)/len(tablet_ids):.1f}%)")
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print(f"Saved to {OUT_PATH}")


if __name__ == "__main__":
    main()
