"""ATF (line-numbered, @obverse/@reverse-tagged) -> {raw transliteration,
Unicode cuneiform signs} per line. Ported from CuneiML's own converter
(github.com/taineleau/CuneiML, CC0), the same tool used to build CuneiML's
own 'signs' field -- so tablets we pull from the raw CDLI ATF dump / eBL get
signs through the identical process as the rest of the corpus, instead of
sitting with an empty 'signs' column (session 2026-08-12: user asked to
unify signs+transliteration across all sources rather than leaving the
bulk-backfilled tablets text-only).

Vendored sign list: data/raw/cuneiform_unicode_vocab/{token.tsv,
cuneiform_vocab.txt} (8200 entries total, copied verbatim from the CuneiML
repo, same CC0 license). No network calls, no external API.
"""
import os
import re
from collections import Counter

VOCAB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "raw", "cuneiform_unicode_vocab",
)

_FACE_KEYS = ("obverse", "reverse", "left", "right", "top", "down", "surface a")


def _load_vocab():
    text2sign = {}
    for fname in ("cuneiform_vocab.txt", "token.tsv"):
        path = os.path.join(VOCAB_DIR, fname)
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip("\n")
                if not line:
                    continue
                try:
                    k, s = line.split("\t")
                except ValueError:
                    continue
                text2sign[k] = s
    return text2sign


_TEXT2SIGN = _load_vocab()

_S_TOKENS = ("<B>", "<M>", "<S>", "<D>", "<munus>", "<ansze>", "<ki>", "<disz>", "x")


def _remove_at(x):
    if x.endswith("@c)") or x.endswith("@t)"):
        return x[:-3] + ")"
    return None


def _remove_spaces(signs):
    out = []
    for item in signs:
        if item == "<S>" and out and out[-1] == "<S>":
            continue
        out.append(item)
    return out


def atf_to_lines(raw_text):
    """raw_text: a tablet's full ATF body (line-numbered, with @face/#atf/$
    structural markers) -- e.g. one &P###### chunk's body from the CDLI bulk
    dump, or an eBL fragment's 'atf' field, unmodified.

    Returns a list of {'raw': str, 'signs': [str], 'num': str, 'face': str}
    in file order, plus an unknown-token miss count for QA (CuneiML's own
    paper reports ~1% of tokens fail to resolve to a sign)."""
    lines_out = []
    curr_face = "default"
    misses = Counter()
    total_tokens = 0

    # qpc (Proto-Elamite) and qpe (Linear Elamite) transliterate as
    # catalogued sign-numbers (e.g. "M157", "1(N14)"), not real cuneiform
    # syllables -- _TEXT2SIGN can't resolve them, and a handful of numeral
    # tokens (Nxx) accidentally DO resolve, letting garbled M/N fragments
    # leak into 'text' instead of being cleanly dropped as empty (session
    # 2026-08-22 finding: 200 documents in the corpus were contaminated
    # this way before this check existed). Every other ATF lang code
    # observed in our sources (sux, akk, qeb/Eblaite, xhu, qcu, urartian,
    # hit, uga, ...) is genuine syllabic cuneiform and parses correctly, so
    # this is a targeted exclusion, not a blanket non-akk/sux filter.
    if re.search(r"(?m)^#atf:\s*lang\s+(qpc|qpe)\b", raw_text):
        return [], Counter(), 0

    sep = "\n"
    if "\\n" in raw_text and "\n" not in raw_text:
        sep = "\\n"

    for line in raw_text.split(sep):
        line = line.strip()
        if not line:
            continue
        if line.startswith("&") or line.startswith("'&"):
            continue
        if line.startswith("#atf"):
            continue
        if line.startswith("#") or line.startswith(">>"):
            continue
        if line.startswith("$"):
            continue
        if line.startswith("@"):
            key = line[1:].strip().strip("?")
            if key in _FACE_KEYS:
                curr_face = key
            continue

        line = line.replace("($ blank space $)", "<S>").replace("_", " ")

        parts = line.split(". ")
        if len(parts) < 2:
            continue
        if len(parts) > 2:
            parts = parts[0], ". ".join(parts[1:])
        line_num, body = parts

        # Two derived strings from here, both starting from the same
        # unmutated `body` so they stay aligned:
        #  - raw_out: the flattened transliteration this project actually
        #    trains/displays on downstream (every caller re-applies
        #    prepare_hf_dataset.py's clean_transliteration on top of this
        #    function's "raw" output). Must match that function's own
        #    documented policy: {...} determinatives -- of ANY kind, {d}
        #    included, no special case -- dropped entirely with no trace
        #    (matches _DETERMINATIVE_RE, and Lazar et al. 2021's own
        #    treatment of the same markup); [...] editorial restorations
        #    keep their content, only the bracket characters themselves
        #    are stripped (matches _BRACKET_CHARS -- Section 3 of the
        #    paper draft states restorations are kept, not discarded, for
        #    the exact same reason Aeneas gives). An earlier version of
        #    this function instead mapped {d} to a bare literal "D" glued
        #    onto the next word with no separator (found via the web demo:
        #    "Dmarduk" tokenizing as "D"+"##mar"+"##duk" instead of one
        #    clean word) and deleted [...] spans outright, content and
        #    all, silently producing orphaned word-fragments with no gap
        #    marker in their place -- both inconsistent with the policy
        #    actually documented for the rest of the corpus.
        #  - sign_src: aggressively stripped (same recipe as before),
        #    used only to look up Unicode signs below -- losing bracket/
        #    brace content here doesn't matter, since 'signs' is a
        #    display-only column, never the model's actual text input.
        raw_out = re.sub(r"\{[^}]*\}", "", body)
        raw_out = raw_out.translate(str.maketrans("", "", "[]"))
        # ATF "!(X)" = the sign was collated/corrected; X is the scribe's
        # original (rejected) reading, not real content -- drop the whole
        # unit together. Must happen before the standalone "!" strip below,
        # or clean_transliteration's generic ()-stripping (which keeps
        # parenthesized content, e.g. for a genuine alternate reading) has
        # no way to tell this case apart and keeps "X" glued onto the
        # previous word with no separator (e.g. "man-za-zu!(SU)" ->
        # "man-za-zuSU").
        raw_out = re.sub(r"!\([^)]*\)", "", raw_out)
        # "#"/"?"/"!" are ATF certainty flags glued directly onto a sign
        # (damaged-but-probable / uncertain reading / collated correction)
        # -- not meaningful transliteration content on their own, and not
        # covered by clean_transliteration downstream, so they must be
        # dropped here or they leak into training text as stray characters
        # (e.g. "lu-u#", "kip#-pa#-ti₃#").
        raw_out = raw_out.replace("#", "").replace("?", "").replace("!", "")

        sign_src = body.replace("{d}", "")
        for x in re.findall(r"\{.*?\}", sign_src):
            sign_src = sign_src.replace(x, " " + x[1:-1] + " ")
        sign_src = sign_src.replace("#", "").replace("?", "").replace("!", "")
        for x in re.findall(r"\[.*?\]", sign_src):
            sign_src = sign_src.replace(x, "")

        text = sign_src
        tokens = text.split(" ")
        signs = []
        for i, t in enumerate(tokens):
            if i > 0 and len(signs) > 0:
                signs.append("<S>")
            if "-" in t:
                for x in t.split("-"):
                    x = x.strip()
                    if not x:
                        continue
                    total_tokens += 1
                    if x in _TEXT2SIGN:
                        signs.append(_TEXT2SIGN[x])
                    else:
                        alt = _remove_at(x)
                        if alt and alt in _TEXT2SIGN:
                            signs.append(_TEXT2SIGN[alt])
                        else:
                            misses[x] += 1
            elif t in _TEXT2SIGN:
                total_tokens += 1
                signs.append(_TEXT2SIGN[t])
            elif t in _S_TOKENS:
                total_tokens += 1
                signs.append(t)
            elif t.strip():
                total_tokens += 1
                alt = _remove_at(t)
                if alt and alt in _TEXT2SIGN:
                    signs.append(_TEXT2SIGN[alt])
                else:
                    misses[t] += 1

        signs = _remove_spaces(signs)
        raw_out = re.sub(r"\s+", " ", raw_out).strip()
        if text.strip() and raw_out:
            lines_out.append({"raw": raw_out, "signs": signs, "num": line_num.strip(), "face": curr_face})

    return lines_out, misses, total_tokens
