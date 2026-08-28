"""English-translation backfill from three offline sources:

1. CDLI's own '#tr.en:' ATF comment lines (~5.4k tablets).
2. eBL's identical convention in its own 'atf' field (~1.4k fragments).
3. A third-party cache of ORACC's *rendered HTML* pages (25,903 of them --
   github.com/shaharspencer/oracc-parser's Zenodo data dump, record
   20625379). ORACC itself ships none of this in its downloadable
   corpusjson packages -- checked directly earlier (a RINAP composite-text
   file, which is where ORACC keeps a translation for texts that have one,
   carries full morphological analysis but no translation node at all) --
   the live site renders translations from data that simply isn't in the
   bulk JSON download. This HTML cache is the missing piece: each page's
   interlinear table has a 'p.tr' cell per line with the same translation
   ORACC's own web viewer shows.

Coverage before source 3 was modest (~2.5-5% of documents, concentrated in
already-published literary/royal-inscription texts) -- most administrative/
economic tablets were never translated by anyone, and that's still true
after it; source 3 mainly recovers ORACC-edited literary/royal-inscription
corpora (RINAP, CMAwR, SAAo, ...) that CDLI/eBL don't carry a translation
for even when they host the same text.

Adds one new 'translation' column (empty string where none found) to
hf_dataset / hf_dataset_documents_with_cdli_bulk / hf_dataset_signs_translit
in place, keyed by tablet_id (works regardless of which pipeline originally
produced that row -- CDLI-bulk, ORACC, or CuneiML -- since ORACC/CuneiML
rows often carry a real CDLI P-number too, via prepare_oracc.py's cdli_id /
prepare_cuneiml.py's P-number normalization).

Run:  python -m src.data_pipeline.backfill_translations
"""
import csv
import io
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

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CDLI_ATF_PATH = os.path.join(BASE_DIR, "data", "raw", "cdli_bulk", "cdliatf_unblocked.atf")
EBL_PATH = os.path.join(BASE_DIR, "data", "raw", "cdli_bulk", "ebl_fragments.json")
ORACC_HTML_ZIP = os.path.join(BASE_DIR, "data", "raw", "oracc_html_translations.zip")
ORACC_CATALOGUES_ZIP = os.path.join(BASE_DIR, "data", "raw", "oracc_catalogues.zip")
TRANSLATIONS_CACHE = os.path.join(BASE_DIR, "data", "interim", "translations.json")

DATASET_DIRS = [
    os.path.join(BASE_DIR, "data", "processed", "hf_dataset"),
    os.path.join(BASE_DIR, "data", "processed", "hf_dataset_documents_with_cdli_bulk"),
    os.path.join(BASE_DIR, "data", "processed", "hf_dataset_signs_translit"),
]

_P_HEADER_RE = re.compile(r"^&P(\d+)")
# CDLI's own line-wrap artifact: ". . ." with each inter-period space widened
# to a non-breaking space by their publishing pipeline -- not a real gap
# marker in the sense mark_damage_signals() cares about (this is prose, not
# transliteration), just visual noise once \xa0 renders as a literal space
# in a normal browser. Collapses any run of 2+ periods (any spacing) to one
# ellipsis character.
_ELLIPSIS_RE = re.compile(r"(?:\.[ \xa0]*){2,}")
# CDLI/eBL's inline style markup ('@i{...}' = italic, '@sux{...}'/'@akk{...}'
# = a Sumerian/Akkadian-language span, conventionally also rendered in
# italics, '_..._' = emphasis) -- meant for a renderer, not to be shown
# verbatim to a reader. Non-greedy content match: doesn't handle the rare
# nested-brace case (a determinative cited inside an @sux{} span) perfectly,
# but that's a handful of documents out of ~1500 non-empty translations.
_MARKUP_RE = re.compile(r"@(?:i|sux|akk)\{([^{}]*)\}")
_EMPHASIS_RE = re.compile(r"_([^_]+)_")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_translation(text: str) -> str:
    if not text:
        return ""
    text = _CONTROL_CHAR_RE.sub("", text)
    text = _ELLIPSIS_RE.sub("…", text)
    text = _MARKUP_RE.sub(r"<i>\1</i>", text)
    text = _EMPHASIS_RE.sub(r"<i>\1</i>", text)
    return re.sub(r"[ \xa0]+", " ", text).strip()


def _atf_translation_lines(atf_text: str) -> str:
    """All '#tr.en:' lines in one ATF body, joined in document order --
    same whole-document flattening precedent as the 'text'/'signs' columns
    (no per-line alignment kept)."""
    lines = []
    for line in atf_text.split("\n"):
        line = line.strip()
        if line.startswith("#tr.en:"):
            txt = line[len("#tr.en:"):].strip()
            if txt:
                lines.append(txt)
    return " ".join(lines)


def extract_cdli_bulk_translations(path: str) -> dict[str, str]:
    translations: dict[str, str] = {}
    current_id, current_lines = None, []

    def flush():
        if current_id and current_lines:
            translations[current_id] = clean_translation(" ".join(current_lines))

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _P_HEADER_RE.match(line)
            if m:
                flush()
                current_id, current_lines = f"P{m.group(1)}", []
            elif line.strip().startswith("#tr.en:"):
                txt = line.strip()[len("#tr.en:"):].strip()
                if txt:
                    current_lines.append(txt)
        flush()
    return translations


def extract_ebl_translations(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as f:
        frags = json.load(f)
    translations: dict[str, str] = {}
    for frag in frags:
        atf = frag.get("atf") or ""
        if "#tr.en:" not in atf:
            continue
        tr = clean_translation(_atf_translation_lines(atf))
        if not tr:
            continue
        # Same tablet_id convention as add_showcase_texts.py: the real CDLI
        # P-number when eBL cross-references one (most literary fragments
        # do), else eBL's own "ebl:{_id}" fallback.
        cdli = (frag.get("externalNumbers") or {}).get("cdliNumber")
        tid = cdli or f"ebl:{frag.get('_id')}"
        translations[tid] = tr
    return translations


_ORACC_P_NUMBER_RE = re.compile(r"^P\d+$")
# Each translation cell's own leading citation, e.g. "(1.1.1:33')", "(Sigla)",
# or "(o 001)" (space-separated face+line, EA-letters style) -- a line/
# witness reference for a print edition, not prose; meaningful in the
# interlinear HTML table it came from, just noise once every line's
# translation is flattened into one whole-document string.
_ORACC_LABEL_RE = re.compile(r"^\([^)]*\)\s*")


def _extract_translation_from_html(html: bytes) -> str:
    soup = BeautifulSoup(html, "html.parser")
    parts = []
    for cell in soup.select("p.tr"):
        text = _ORACC_LABEL_RE.sub("", cell.get_text(" ", strip=True))
        if text and text != "(break)":
            parts.append(text)
    joined = re.sub(r"\s+", " ", " ".join(parts))
    # get_text(' ', ...) inserts a space at every tag boundary, which
    # includes the boundary right inside a bracket/parenthesis -- collapse
    # "[ ... ]" / "( ... )" back to how a human would actually write it.
    joined = re.sub(r"\s+([.,;:!?)\]])", r"\1", joined)
    joined = re.sub(r"([(\[])\s+", r"\1", joined)
    return clean_translation(joined)


def _init_html_worker(zip_path: str) -> None:
    global _HTML_ZIP
    _HTML_ZIP = zipfile.ZipFile(zip_path)


def _process_html_file(name: str) -> tuple[str, str]:
    return name, _extract_translation_from_html(_HTML_ZIP.read(name))


def _build_oracc_cdli_crossref(path: str) -> dict[str, str]:
    """{"{project_path}:{textid}" -> real CDLI P-number}, from the same
    Zenodo dump's per-project catalogue CSVs -- covers texts whose HTML
    page is filed under an internal ORACC id (X000001, Q004184, ...) but
    that ORACC's own catalogue cross-references to a real CDLI tablet, so
    a CDLI-bulk/CuneiML-sourced row for the same physical tablet benefits
    from the ORACC translation too, not just pure-ORACC-sourced rows."""
    crossref: dict[str, str] = {}
    z = zipfile.ZipFile(path)
    for name in z.namelist():
        if not name.endswith(".csv"):
            continue
        project_path = name[:-len(".csv")].replace("-", "/")
        with z.open(name) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))
            for row in reader:
                text_id = row.get("text_id") or row.get("id_text")
                cdli = row.get("id_text") if _ORACC_P_NUMBER_RE.match(row.get("id_text") or "") else None
                if text_id and cdli and text_id != cdli:
                    crossref[f"{project_path}:{text_id}"] = cdli
    return crossref


def extract_oracc_html_translations(html_zip: str, catalogues_zip: str, workers: int = 10) -> dict[str, str]:
    crossref = _build_oracc_cdli_crossref(catalogues_zip)
    names = [n for n in zipfile.ZipFile(html_zip).namelist() if n.endswith(".html")]
    # A P-number filename isn't unique across projects -- e.g. Amarna
    # letters are independently hosted under both 'aemw/amarna' and
    # 'contrib/amarna' with the same P271129.html name, one carrying a real
    # translation and the other empty (~465 such collisions total). Keep
    # the longest text per key rather than "whichever imap_unordered
    # happens to finish last" -- the old approach only guarded against an
    # empty duplicate overwriting a real one, not a shorter/worse one
    # overwriting a better one, and gave a different (non-deterministic)
    # answer depending on multiprocessing scheduling.
    translations: dict[str, str] = {}
    with mp.Pool(processes=workers, initializer=_init_html_worker, initargs=(html_zip,)) as pool:
        for name, text in tqdm(pool.imap_unordered(_process_html_file, names, chunksize=25), total=len(names)):
            if not text:
                continue
            # project/sub/proj/TEXTID.html -> "project/sub/proj", "TEXTID"
            project_path, textid = name.rsplit("/", 1)
            textid = textid[:-len(".html")]
            keys = [textid] if _ORACC_P_NUMBER_RE.match(textid) else [f"oracc:{project_path}:{textid}"]
            if not _ORACC_P_NUMBER_RE.match(textid):
                cdli = crossref.get(f"{project_path}:{textid}")
                if cdli:
                    keys.append(cdli)
            for key in keys:
                if key not in translations or len(text) > len(translations[key]):
                    translations[key] = text
    return translations


def build_translation_lookup() -> dict[str, str]:
    print("Extracting CDLI-bulk translations...")
    cdli_tr = extract_cdli_bulk_translations(CDLI_ATF_PATH)
    print(f"  {len(cdli_tr)} tablets")
    print("Extracting eBL translations...")
    ebl_tr = extract_ebl_translations(EBL_PATH)
    print(f"  {len(ebl_tr)} fragments")
    print("Extracting ORACC HTML-cache translations...")
    oracc_tr = extract_oracc_html_translations(ORACC_HTML_ZIP, ORACC_CATALOGUES_ZIP)
    print(f"  {len(oracc_tr)} texts")
    # ORACC preferred on overlap -- for a text ORACC has itself edited
    # (RINAP, CMAwR, SAAo, ...), its own translation is the primary
    # scholarly source; eBL preferred over bare CDLI next, same precedent
    # as demo_predictions.py's fetch_record().
    merged = {**cdli_tr, **ebl_tr, **oracc_tr}
    print(f"Merged: {len(merged)} unique tablet_ids")
    os.makedirs(os.path.dirname(TRANSLATIONS_CACHE), exist_ok=True)
    with open(TRANSLATIONS_CACHE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    return merged


def apply_to_datasets(translations: dict[str, str]) -> None:
    # datasets refuses to save_to_disk back onto the same path a dataset was
    # loaded from -- write to a sibling dir and swap it into place instead.
    import shutil

    for ds_dir in DATASET_DIRS:
        print(f"\n=== {ds_dir} ===")
        ds = load_from_disk(ds_dir)
        for split in ds.keys():
            n = len(ds[split])
            matched = sum(1 for tid in ds[split]["tablet_id"] if tid in translations)
            ds[split] = ds[split].map(
                lambda ex: {"translation": translations.get(ex["tablet_id"], "")},
                desc=f"translation ({split})",
            )
            print(f"  {split}: {n} rows, {matched} matched ({100*matched/n:.2f}%)")
        tmp_dir = ds_dir + "_NEW"
        ds.save_to_disk(tmp_dir)
        shutil.rmtree(ds_dir)
        os.rename(tmp_dir, ds_dir)
        print(f"  saved to {ds_dir}")


if __name__ == "__main__":
    lookup = build_translation_lookup()
    apply_to_datasets(lookup)
