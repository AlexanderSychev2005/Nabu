import json
import os
import re
import sys
import random
from pathlib import Path
from typing import Optional

from tqdm import tqdm
from datasets import Dataset, DatasetDict, Features, Sequence, Value, ClassLabel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# 'signs' (Unicode cuneiform) and 'text' (cleaned Latin transliteration) both
# live in the same row; train_mbert.py tokenizes 'text' at load time with
# mBERT's own WordPiece tokenizer. Neither side is pre-tokenized here.

_DETERMINATIVE_RE = re.compile(r"\{[^}]*\}")  # e.g. {m}, {d}, {ki} -- editorial determinatives, dropped entirely (Lazar et al. 2021 do the same with sub/superscripts)
_SCRIBAL_ERROR_RE = re.compile(r"<<[^>]*>>")  # ATF: sign(s) an editor considers an erroneous scribal addition -- unlike <x> (accidentally omitted, restored by the editor, real content), <<x>> should be dropped entirely, content included, not just the doubled brackets
# ATF inline editorial note (e.g. "$erasure?$", "$copy: TA*$", "$;:GE23$") --
# ORACC and CDLI both use $-delimited asides for scribal/collation remarks
# that were never meant to be running transliteration text. Must run before
# _BRACKET_CHARS strips '$'-adjacent brackets, or a comment glued onto a
# word survives as stray punctuation once the delimiters are gone.
_COMMENT_RE = re.compile(r"\$[^$]*\$")
# Editorial uncertainty/restoration brackets (every Unicode half-bracket
# variant actually observed in the corpus, not just the canonical ⸢⸣) and
# ATF's sign-separator (|) -- stripped, content kept (single <x> only --
# <<x>> is handled separately above, before this runs). '{}' is included
# here too, not just in _DETERMINATIVE_RE: that regex only removes
# well-paired {det}s, but ORACC builds 'text' by concatenating each word's
# own transliteration fragment, and a determinative/language-shift span
# that (in ORACC's own markup) spans multiple words can lose its matching
# half in the join -- e.g. "AN } um-ma" for Amarna-letter divine names --
# leaving an orphaned brace with no partner. A lone {/} is never itself
# real transliteration content, so stripping any survivor here is safe.
_BRACKET_CHARS = "[]⸢⸣⌈⌉˹˺(){}<>|"
_SUBSCRIPT_DIGITS = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
_ASCII_INDEX_DIGIT_RE = re.compile(r"([a-zŋ])([0-9]+)(?![0-9a-z])")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # stray encoding-artifact control characters (e.g. a lone \x8a) -- never real content
# ORACC's own "sign present but illegible" placeholder is uppercase X
# (word-delimited) / U+2093 subscript-x (used for numerals of unknown
# value); CDLI-bulk's equivalent is lowercase x (see cuneiform_unicode.py's
# _S_TOKENS). train_mbert.py's mark_damage_signals() only recognizes the
# lowercase form (LONE_X_RE = r"\bx\b"), so without this normalization an
# ORACC-sourced 'X' never becomes the [unused1] damage sentinel -- it's
# ordinary text to the tokenizer, eligible to be an MLM mask *target* even
# though there is no real answer for what an illegible sign "should" be.
_STANDALONE_X_RE = re.compile(r"\bX\b")
# ATF certainty flag ('sign confirmed by collation'), glued directly onto a
# sign the same way '#'/'?'/'!' are -- those three are already stripped for
# CDLI-bulk text by cuneiform_unicode.py before it ever reaches here, but
# that cleaning never runs on ORACC's 'frag' strings, and '*' isn't in that
# function's own strip list either. Like '#'/'?'/'!', it's never itself a
# real sign, so an unconditional strip is safe.

def _normalize_cuneiml_romanization(text: str) -> str:
    """CuneiML and ORACC transliterate the same phonemes with two disjoint
    ASCII/Unicode conventions (measured on 200k lines each: ORACC uses
    Unicode š/Unicode subscripts in ~49%/62% of lines and ASCII 'sz'/
    digit-suffix in ~0%/0.3%; CuneiML is the mirror image -- ASCII 'sz'/
    digit-suffix in ~62%/75%, Unicode š in ~0%). Left unmerged, the same
    word surfaces as two unrelated tokens depending on source, which
    fragments mBERT's WordPiece vocabulary for no linguistic reason. Digit-
    suffix conversion must run before the digraph substitutions below, so
    that a digit right after a still-ASCII 'sz'/'s,'/'t,' (e.g. 'gesz2')
    gets subscripted before the digraph collapses ('gesz2' -> 'gesz₂' ->
    'geš₂', not 'geš2'). ASCII 'h' for ḫ is NOT converted: CuneiML
    never marks that distinction (0 instances found), so it can't be
    recovered from the ASCII side.
    """
    text = _ASCII_INDEX_DIGIT_RE.sub(lambda m: m.group(1) + m.group(2).translate(_SUBSCRIPT_DIGITS), text)
    text = text.replace("sz", "š").replace("SZ", "Š").replace("Sz", "Š")
    text = text.replace("s,", "ṣ").replace("S,", "Ṣ")
    text = text.replace("t,", "ṭ").replace("T,", "Ṭ")
    return text

def clean_transliteration(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = _normalize_cuneiml_romanization(raw)
    text = _COMMENT_RE.sub("", text)
    text = _DETERMINATIVE_RE.sub("", text)
    text = _SCRIBAL_ERROR_RE.sub("", text)
    text = text.translate(str.maketrans("", "", _BRACKET_CHARS))
    text = _CONTROL_CHAR_RE.sub("", text)
    text = text.replace("*", "").replace("\\", "")
    text = _STANDALONE_X_RE.sub("x", text).replace("ₓ", "x")
    # Em-dash/en-dash/figure-dash used as a name/word-joining mark (e.g.
    # 'GAL—MU', 'qur-di—DINGIR-ma') -- not in mBERT's vocab (unlike every
    # other Assyriological diacritic we checked), so normalize every dash
    # variant to the plain hyphen already used for the same joining role
    # elsewhere in the transliteration.
    text = re.sub(r"[—–‒]", "-", text)
    # Curly apostrophe and the Sumerian ĝ variant: same phoneme as the
    # straight apostrophe/ŋ already used elsewhere in the corpus -- unify
    # so the tokenizer doesn't see two unrelated tokens for one sound.
    text = text.replace("’", "'").replace("ĝ", "ŋ")
    # A dropped {determinative}/$comment$/<<scribal error>> sitting directly
    # between two hyphens (e.g. "pa-<<da>>-aš" -> "pa--aš") leaves a
    # doubled hyphen once its content is gone -- collapse it so the
    # tokenizer doesn't see a fake empty syllable.
    text = re.sub(r"-{2,}", "-", text)
    return re.sub(r"\s+", " ", text).strip()

# --- LABEL ENGINEERING V3.0 MAPPINGS ---
# Substring-based (not exact-match) on purpose: raw CDLI/ORACC values almost
# always carry a "(ca. NNNN-NNNN BC)" or "(mod. Xxx)" suffix, so exact-match
# lists silently dropped ~30-58% of records with a perfectly good value to
# 'Unknown' (verified against the full 353k-row CDLI catalogue and the
# 639k-record merged corpus -- see conversation for the breakdown).

FAKE_GENRE_MARKERS = ('fake (modern)',)

def map_language(l: Optional[str]) -> str:
    if not l: return 'Unknown'
    l = l.lower()
    if 'bilingual' in l or ('sumerian' in l and 'akkadian' in l): return 'Bilingual'
    if 'akkadian' in l or 'assyrian' in l or 'babylonian' in l: return 'Akkadian'
    if 'sumerian' in l: return 'Sumerian'
    if any(x in l for x in ['urartian', 'hittite', 'eblaite', 'elamite', 'old persian', 'ugaritic']): return 'Peripheral/Other'
    # Small counts individually (dozens-low hundreds), but all genuinely
    # real, attested ancient Near Eastern languages that plainly belong in
    # this bucket's own stated scope, not in 'undetermined'/'uncertain'
    # (which stay Unknown, correctly).
    if any(x in l for x in ['persian', 'aramaic', 'hebrew', 'hurrian']): return 'Peripheral/Other'
    return 'Unknown'

def map_period(p: Optional[str]) -> str:
    if not p: return 'Unknown'
    p = p.lower()
    if 'neo-assyrian' in p or 'neo assyrian' in p: return 'Neo-Assyrian'
    if 'ur iii' in p: return 'Ur III'
    if 'old assyrian' in p: return 'Old Assyrian'
    if 'old babylonian' in p: return 'Old Babylonian'
    if 'middle assyrian' in p: return 'Middle Assyrian'
    if 'middle babylonian' in p: return 'Middle Babylonian'
    if 'neo-babylonian' in p or 'neo/late babylonian' in p or 'late babylonian' in p: return 'Neo-Babylonian'
    if any(x in p for x in ['ed iii', 'ed i-ii', 'early dynastic', 'old akkadian', 'lagaš ii', 'lagash ii', 'ebla', 'uruk iii', 'uruk iv']): return 'Third Millennium'
    if any(x in p for x in ['seleucid', 'achaemenid', 'hellenistic']): return 'Late Antiquity'
    return 'Unknown'

def map_genre(g: Optional[str]) -> str:
    if not g: return 'Unknown'
    g = g.lower()
    if g in FAKE_GENRE_MARKERS: return 'Unknown'
    if 'administrative' in g: return 'Administrative'
    if 'lexical' in g: return 'Lexical'
    if 'royal' in g or 'monumental' in g: return 'Royal Inscriptions'
    if any(x in g for x in ['literary', 'scholarly', 'astrolog', 'astronomical', 'omen', 'school',
                             'ritual', 'incantation', 'extispicy', 'mathematical', 'scientific',
                             'technical procedure', 'prayer']): return 'Literary & Scholarly'
    if any(x in g for x in ['legal', 'treaty', 'grant']): return 'Legal'
    if 'letter' in g: return 'Letters'
    return 'Unknown'

def map_provenience(p: Optional[str]) -> str:
    if not p: return 'Unknown'
    p = p.lower()
    if 'nineveh' in p or 'kuyunjik' in p: return 'Nineveh'
    if 'umma' in p: return 'Umma'
    if 'girsu' in p or 'tello' in p: return 'Girsu'
    if 'nippur' in p or 'nuffar' in p: return 'Nippur'
    if 'puzriš-dagan' in p or 'puzris-dagan' in p or 'drehem' in p: return 'Puzriš-Dagan'
    if 'kanesh' in p or 'kültepe' in p: return 'Kanesh'
    if 'aššur' in p or 'assur' in p or 'ashur' in p or 'qal' in p and 'sherqat' in p: return 'Assur'
    if 'uruk' in p or 'warka' in p: return 'Uruk'
    if p.startswith('ur ') or p.startswith('ur(') or 'tell muqayyar' in p or p == 'ur': return 'Ur'
    if 'ugarit' in p or 'ras shamra' in p: return 'Ugarit'
    if 'sippar' in p: return 'Sippar'
    if 'nimrud' in p or 'kalhu' in p: return 'Nimrud'
    # These are all major, well-attested single findspots -- catalogue
    # counts overstate how many end up in the actual corpus, though, since
    # most CDLI records have no recoverable transliteration anywhere in
    # our sources -- only candidates that cleared >=50 actual documents
    # after text recovery are kept here (a few catalogue-large sites, e.g.
    # Ašnakkum, Lagash, Qattara, ended up with single digits to zero and
    # were dropped).
    if 'hattus' in p or 'boğazk' in p or 'bogazk' in p: return 'Hattusa'
    if p.startswith('mari ') or p.startswith('mari(') or p == 'mari' or 'tell hariri' in p: return 'Mari'
    if 'ebla' in p or 'tell mardikh' in p: return 'Ebla'
    if 'susa' in p or 'shush' in p: return 'Susa'
    # 'babylonia'/'babylonian' (the broader, often-uncertain region) must
    # NOT count as the specific city -- "uncertain (mod. Babylonia)" is
    # explicitly marking the findspot as unknown, not asserting Babylon.
    if ('babylon' in p and 'babylonia' not in p) or 'babili' in p: return 'Babylon'
    if 'nuzi' in p or 'gasur' in p: return 'Nuzi'
    if 'irisagrig' in p: return 'Irisagrig'
    if 'persepolis' in p or 'pārśa' in p or 'parsa' in p: return 'Persepolis'
    if 'kish' in p: return 'Kish'
    if 'larsa' in p: return 'Larsa'
    if 'garšana' in p or 'garsana' in p: return 'Garšana'
    if 'emar' in p or 'tell meskene' in p: return 'Emar'
    if 'isin' in p: return 'Isin'
    if 'ešnunna' in p or 'esnunna' in p or 'tell asmar' in p: return 'Ešnunna'
    if 'šaduppum' in p or 'saduppum' in p: return 'Šaduppum'
    if 'nerebtum' in p: return 'Nerebtum'
    if 'šuruppak' in p or 'suruppak' in p: return 'Šuruppak'
    if 'kisurra' in p: return 'Kisurra'
    if 'adab' in p or 'bismaya' in p: return 'Adab'
    if 'huzirina' in p or 'sultantepe' in p: return 'Huzirina'
    if 'pī-kasî' in p or 'pi-kasi' in p or 'tell abu antiq' in p: return 'Pī-Kasî'
    if 'tuttul' in p or "tell bi'a" in p or 'tell bia' in p: return 'Tuttul'
    if 'akhetaten' in p or 'amarna' in p: return 'Amarna'
    if 'zabalam' in p: return 'Zabalam'
    return 'Unknown'

LANGUAGE_LABELS = ['Akkadian', 'Sumerian', 'Bilingual', 'Peripheral/Other']
PERIOD_LABELS = ['Neo-Assyrian', 'Ur III', 'Old Babylonian', 'Old Assyrian', 'Middle Assyrian', 'Middle Babylonian', 'Neo-Babylonian', 'Third Millennium', 'Late Antiquity']
GENRE_LABELS = ['Administrative', 'Lexical', 'Royal Inscriptions', 'Literary & Scholarly', 'Legal', 'Letters']
# Candidate period classes tied to the provenience expansion below (Middle
# Hittite, Proto-/Neo-/Middle Elamite) were tried and dropped: after actual
# text recovery they landed at 3/2/0/0 documents respectively, useless for
# a classification head -- catalogue-scale counts (the Middle Hittite value
# alone had 14.7k rows) just don't survive the "does a transliteration
# exist anywhere in our sources" filter for this particular period field.
PROVENIENCE_LABELS = ['Nineveh', 'Umma', 'Girsu', 'Nippur', 'Puzriš-Dagan', 'Kanesh', 'Assur', 'Uruk', 'Ur', 'Ugarit', 'Sippar', 'Nimrud',
                      'Hattusa', 'Mari', 'Ebla', 'Susa', 'Babylon', 'Nuzi', 'Irisagrig', 'Persepolis', 'Kish', 'Larsa', 'Garšana',
                      'Emar', 'Isin', 'Ešnunna', 'Šaduppum', 'Nerebtum', 'Šuruppak', 'Kisurra',
                      'Adab', 'Huzirina', 'Pī-Kasî', 'Tuttul', 'Amarna', 'Zabalam']

def label_to_idx(label_str: Optional[str], label_list: list[str]) -> int:
    if not label_str or label_str == 'Unknown':
        return -100
    try:
        return label_list.index(label_str)
    except ValueError:
        return -100

def _split_ids(tablet_id: str) -> list[str]:
    """ORACC's own catalogue occasionally joins several physical P-numbers
    into one tablet_id ("P1; P2; P3", curator-noted joins/exemplar groups).
    Every identity check below (cross-source dedup, split grouping) must
    compare atomic P-numbers, not the joined string, or a source that
    (correctly) treats each P-number as its own document silently
    duplicates -- and can leak across train/val/test -- content already
    covered by the joined row."""
    return [p.strip() for p in tablet_id.split(";")] if ";" in tablet_id else [tablet_id]


def _load_photo_atoms() -> set[str]:
    """P-numbers (no leading zeros) that have a real photo in the vision
    manifest -- used to steer _canonical_group_keys's representative choice
    away from orphaning a photo (see its own docstring)."""
    manifest = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "vision_dataset", "manifest.jsonl",
    )
    if not os.path.exists(manifest):
        return set()
    atoms = set()
    with open(manifest, encoding="utf-8") as f:
        for line in f:
            try:
                atoms.add("P" + json.loads(line)["id"].lstrip("0"))
            except Exception:
                pass
    return atoms


def _canonical_group_keys(tablet_ids: list, prefer: Optional[set] = None) -> dict:
    """Union-Find over atomic P-numbers: two tablet_id strings sharing any
    atom must resolve to the same canonical key, so grouping/splitting by
    tablet identity treats them as one physical document. {tablet_id ->
    canonical_key}, tablet_ids repeated verbatim resolve identically.

    The representative picked for each merged group is NOT arbitrary: when
    several physical fragments join into one text, each fragment can carry
    its own separate photo, and the vision pipeline looks up a document's
    photo by its own tablet_id -- picking a photo-less atom as the
    representative would silently orphan a real photo the merge otherwise
    has no way to know it lost (confirmed: 8 of 9 photographed join-groups
    in this corpus would lose their photo without this). `prefer` (atoms
    known to have a photo) is preferred as the representative when present
    in a group; the lexicographically smallest atom breaks every tie, so
    the choice is reproducible run to run."""
    prefer = prefer or set()
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != root:
            parent[x], x = root, parent.get(x, x)
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for tid in tablet_ids:
        atoms = _split_ids(tid)
        for a in atoms:
            parent.setdefault(a, a)
        for a in atoms[1:]:
            union(atoms[0], a)

    members: dict[str, list[str]] = {}
    for tid in tablet_ids:
        for a in _split_ids(tid):
            members.setdefault(find(a), []).append(a)

    representative = {}
    for root, atoms in members.items():
        photographed = sorted(a for a in set(atoms) if a in prefer)
        representative[root] = photographed[0] if photographed else min(atoms)

    return {tid: representative[find(_split_ids(tid)[0])] for tid in tablet_ids}


def load_and_deduplicate_v2(files: list) -> list[dict]:
    print("Loading and deduplicating datasets (v2)...")

    # Canonicalize tablet_id up front, across every input file, before any
    # dedup/grouping below runs: ORACC's own catalogue occasionally joins
    # several physical P-numbers into one tablet_id ("P1; P2; P3"), while
    # another source (CuneiML, or a later backfill stage) may carry one of
    # those same P-numbers as its own standalone tablet_id. Without this,
    # such rows dedup and group independently -- confirmed causing both
    # cross-split leakage and same-split duplicate documents on the
    # rebuilt corpus. Rewriting every row's tablet_id to one shared
    # canonical form here means every dedup/grouping step downstream (in
    # this function and every later pipeline stage that reads its output)
    # just sees one identity, with no separate atom-aware logic needed.
    all_tids = []
    for file_path in files:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    tid = json.loads(line).get('tablet_id')
                except Exception:
                    continue
                if tid:
                    all_tids.append(tid)
    canonical_tid = _canonical_group_keys(all_tids, prefer=_load_photo_atoms())
    print(f"Canonicalized {len(canonical_tid)} tablet_id strings "
          f"({sum(1 for t, k in canonical_tid.items() if t != k)} rewritten to a shared id)")

    # Cross-source dedup: a small number of CuneiML tablets (~1.5% of them)
    # are the same physical tablet as an ORACC edition (same CDLI P-number).
    # Keep the CuneiML side (it carries images/bboxes for later multimodal
    # heads, and tends to have more lines per tablet) and drop the ORACC
    # duplicate here, before the sign-string dedup below -- otherwise the
    # same tablet contributes near-duplicate lines under two different
    # transliteration styles, and could land in different splits later.
    cuneiml_tablet_ids = set()
    for file_path in files:
        if "cuneiml" in str(file_path).lower():
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        tid = json.loads(line).get('tablet_id')
                        if tid:
                            cuneiml_tablet_ids.add(canonical_tid.get(tid, tid))
                    except Exception:
                        pass
    print(f"CuneiML tablet count (preferred on overlap): {len(cuneiml_tablet_ids)}")

    unique_lines = {}
    skipped_cross_source = 0

    for file_path in files:
        print(f"Reading {file_path}...")
        is_oracc = "oracc" in str(file_path).lower()
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in tqdm(f):
                try:
                    data = json.loads(line)
                    if data.get('tablet_id'):
                        data['tablet_id'] = canonical_tid.get(data['tablet_id'], data['tablet_id'])
                    if is_oracc and data.get('tablet_id') in cuneiml_tablet_ids:
                        skipped_cross_source += 1
                        continue
                    signs = data.get('signs', [])
                    sign_str = "".join(signs).strip()
                    # A signless-but-textful line (prepare_oracc.py's own
                    # normalized-edition case, see its docstring) has
                    # nothing to dedup by via sign_str -- fall back to the
                    # raw transliteration text as the dedup key instead of
                    # dropping the line outright. Dropping *both* would
                    # mean the line has no readable content at all.
                    raw_text = (data.get('raw') or '').strip()
                    dedup_key = sign_str or raw_text
                    if not dedup_key: continue
                    # Scoped to (tablet_id, dedup_key), not dedup_key alone:
                    # a bare content key collides constantly across
                    # unrelated tablets (e.g. a single-word line like "dumu"
                    # or a common royal name repeats verbatim throughout the
                    # corpus by design -- formulaic phrases, not duplicate
                    # documents), which used to silently drop that line from
                    # every tablet except whichever one got processed first,
                    # even when the missing line was the tablet's own most
                    # important content (caught via the Enheduanna disc,
                    # P217330, which was losing its own name-line this way).
                    # This key still catches its original purpose: the same
                    # line appearing twice for the *same* document because
                    # of overlapping source files.
                    key = (data.get('tablet_id') or '', dedup_key)

                    # Merge metadata
                    if key in unique_lines:
                        existing = unique_lines[key]
                        existing['provenience'] = data.get('provenience', existing.get('provenience', 'unknown'))
                        existing['language'] = data.get('language', existing.get('language', 'unknown'))
                        if existing.get('period', 'unknown').lower() == 'unknown':
                            existing['period'] = data.get('period', 'unknown')
                        if existing.get('genre', 'unknown').lower() == 'unknown':
                            existing['genre'] = data.get('genre', 'unknown')
                    else:
                        unique_lines[key] = data
                except Exception:
                    pass

    print(f"Skipped (ORACC lines whose tablet is already in CuneiML): {skipped_cross_source}")
    print(f"Total unique lines across datasets: {len(unique_lines)}")
    return list(unique_lines.values())

def to_examples(records: list[dict]) -> list[dict]:
    """Untokenized examples: both 'signs' (cuneiform) and 'text' (cleaned
    transliteration) side by side, plus the 4 metadata labels shared by both
    training pipelines."""
    examples = []
    for data in tqdm(records, desc="Building examples"):
        examples.append({
            "signs": data.get('signs', []),
            "text": clean_transliteration(data.get('raw')),
            # Carried through (unused by either tokenizer) so a later training
            # stage can look up a per-tablet image by id -- e.g.
            # train_mbert.py's --use_image vision branch, which needs to know
            # which physical tablet a line belongs to. Empty string (not
            # None) so the Arrow 'string' column stays uniformly typed;
            # ORACC-sourced rows get their own synthetic "oracc:..." id
            # (see prepare_oracc.py), which simply won't match any collected
            # image -- fine, since only CuneiML tablets have photos.
            "tablet_id": data.get('tablet_id') or "",
            "period_labels": label_to_idx(map_period(data.get('period')), PERIOD_LABELS),
            "genre_labels": label_to_idx(map_genre(data.get('genre')), GENRE_LABELS),
            "language_labels": label_to_idx(map_language(data.get('language')), LANGUAGE_LABELS),
            "provenience_labels": label_to_idx(map_provenience(data.get('provenience')), PROVENIENCE_LABELS),
        })
    return examples

def main() -> None:
    base_dir = Path(r"C:\Programming\akkadian\data")
    interim_dir = base_dir / "interim"
    processed_dir = base_dir / "processed"
    os.makedirs(processed_dir, exist_ok=True)

    files_to_merge = [
        interim_dir / "oracc.jsonl",
        interim_dir / "cuneiml.jsonl"
    ]
    
    # 1. Deduplicate & Merge
    all_unique_records = load_and_deduplicate_v2(files_to_merge)
    
    # 2. Write the merged, deduplicated pool -- combined_unique.jsonl is the
    # source prepare_document_dataset.py groups into documents from.
    combined_path = processed_dir / "combined_unique.jsonl"
    with open(combined_path, 'w', encoding='utf-8') as f:
        for r in all_unique_records:
            r['text'] = clean_transliteration(r.get('raw'))
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


    # 3. Grouped split (90/5/5) -- grouped by tablet_id, not shuffled per line.
    # Lines from the same physical tablet are highly correlated (same
    # formulae, obviously the same period/genre/provenience), so a per-line
    # random split leaks tablet identity across train/val/test and inflates
    # both MLM and classification metrics. tablet_id is already canonical
    # (rewritten above, before dedup) so no separate grouping pass is needed
    # here to unify joined/atom siblings.
    groups = {}
    for r in all_unique_records:
        groups.setdefault(r.get('tablet_id') or r.get('signs', [None])[0], []).append(r)

    group_keys = list(groups.keys())
    random.seed(42)
    random.shuffle(group_keys)

    n = len(all_unique_records)
    test_budget = int(n * 0.05)
    val_budget = int(n * 0.05)

    test_raw, val_raw, train_raw = [], [], []
    for key in group_keys:
        recs = groups[key]
        if len(test_raw) < test_budget:
            test_raw.extend(recs)
        elif len(val_raw) < val_budget:
            val_raw.extend(recs)
        else:
            train_raw.extend(recs)

    print(f"Tablet groups: {len(group_keys)}")
    print(f"Split sizes: Train={len(train_raw)}, Val={len(val_raw)}, Test={len(test_raw)}")
    
    # 4. Save Test split un-tokenized
    test_path = processed_dir / "test.jsonl"
    print(f"Saving un-tokenized test set to {test_path}...")
    with open(test_path, 'w', encoding='utf-8') as f:
        for r in test_raw:
            # save mapped fields for easy eval later
            r['period_mapped'] = map_period(r.get('period'))
            r['genre_mapped'] = map_genre(r.get('genre'))
            r['language_mapped'] = map_language(r.get('language'))
            r['provenience_mapped'] = map_provenience(r.get('provenience'))
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            
    # 5. Process Train and Val (untokenized -- each training script tokenizes
    # its own 'signs' or 'text' column at load time)
    print("Processing Train records...")
    train_processed = to_examples(train_raw)
    print("Processing Val records...")
    val_processed = to_examples(val_raw)

    # Define features
    features = Features({
        'signs': Sequence(Value('string')),
        'text': Value('string'),
        'tablet_id': Value('string'),
        'period_labels': Value('int64'),
        'genre_labels': Value('int64'),
        'language_labels': Value('int64'),
        'provenience_labels': Value('int64'),
    })
    
    dataset_dict = DatasetDict({
        "train": Dataset.from_list(train_processed, features=features),
        "validation": Dataset.from_list(val_processed, features=features)
    })
    
    hf_dir = processed_dir / "hf_dataset"
    print(f"Saving Arrow dataset to {hf_dir}...")
    dataset_dict.save_to_disk(str(hf_dir))
    
    # Save label dictionaries for model config
    label_dicts = {
        'period': {
            'labels': PERIOD_LABELS,
            'id2label': {i: l for i, l in enumerate(PERIOD_LABELS)},
            'label2id': {l: i for i, l in enumerate(PERIOD_LABELS)}
        },
        'genre': {
            'labels': GENRE_LABELS,
            'id2label': {i: l for i, l in enumerate(GENRE_LABELS)},
            'label2id': {l: i for i, l in enumerate(GENRE_LABELS)}
        },
        'language': {
            'labels': LANGUAGE_LABELS,
            'id2label': {i: l for i, l in enumerate(LANGUAGE_LABELS)},
            'label2id': {l: i for i, l in enumerate(LANGUAGE_LABELS)}
        },
        'provenience': {
            'labels': PROVENIENCE_LABELS,
            'id2label': {i: l for i, l in enumerate(PROVENIENCE_LABELS)},
            'label2id': {l: i for i, l in enumerate(PROVENIENCE_LABELS)}
        }
    }
    with open(processed_dir / "label_configs.json", "w", encoding='utf-8') as f:
        json.dump(label_dicts, f, ensure_ascii=False, indent=2)
        
    print("Done!")

if __name__ == "__main__":
    main()
