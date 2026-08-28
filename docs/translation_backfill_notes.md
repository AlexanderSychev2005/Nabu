# Translation backfill

A `translation` column (empty string where none found) was added to
`hf_dataset` / `hf_dataset_documents_with_cdli_bulk` / `hf_dataset_signs_translit`,
keyed by `tablet_id` -- see `src/data_pipeline/backfill_translations.py`.

## Sources used

1. **CDLI-bulk ATF** (`data/raw/cdli_bulk/cdliatf_unblocked.atf`) -- its own
   `#tr.en:` comment lines, immediately below the line they translate.
   5,368 tablets.
2. **eBL fragments** (`data/raw/cdli_bulk/ebl_fragments.json`) -- same
   `#tr.en:` convention in its own `atf` field. 1,421 fragments. Preferred
   over CDLI-bulk on tablet_id overlap (richer/more current edition for
   literary fragments both sources happen to carry).
3. **ORACC's cached rendered HTML pages** -- Zenodo record 20625379
   (`oracc-parser` package's offline data dump,
   github.com/shaharspencer/oracc-parser), specifically
   `oracc_html_translations.zip` (25,903 pages across all 138 ORACC
   projects). **ORACC's own downloadable corpusjson bulk packages carry no
   translation data at all** -- checked directly by inspecting a RINAP
   composite-text corpusjson file: full morphological/lexical analysis
   (`gw`, `cf`, `sense`, `norm`, ...), no translation node anywhere. The
   live oracc.org site renders one from data that simply isn't in that
   download, and oracc.org itself was unreachable from this environment
   (timed out on every attempt, browser and raw HTTP alike -- reachable
   from a normal residential/VPN connection, so likely a block on
   datacenter/cloud IP ranges rather than the site being down). This cached
   HTML dump was the only way to recover it offline. Preferred over both
   other sources on overlap (primary scholarly edition for texts ORACC
   itself edited -- RINAP, CMAwR, SAAo, etc.).
   16,944 texts recovered this way -- by far the largest contribution.

Extraction: BeautifulSoup on each page's `p.tr` cells (one per
transliteration line), multiprocessed (10 workers, ~3 min for all 25,903
pages). Cleanup strips each cell's own leading line/witness citation (e.g.
`(1.1.1:33′)`, `(o 001)`) and fixes the double-space-around-brackets
artifact `get_text(' ', ...)` introduces at tag boundaries.

tablet_id keys: a page's filename is the real CDLI P-number when ORACC's
own catalogue cross-references one (used directly), else the same
`oracc:{project}:{textid}` convention `prepare_oracc.py` already uses (so
CDLI-bulk/CuneiML-sourced rows *and* ORACC-sourced rows both benefit from
the same lookup). CuneiML itself has no translation field of its own to
extract -- checked its raw JSON directly (`id`/`img_url`/`lineart`/`bboxes`/
`text` only) -- but its rows already carry real CDLI P-numbers, so they
join against sources 1-3 automatically without needing separate handling.

## Sources checked and rejected

- **Kaggle "ORACC Akkadian-English Parallel Corpus"**
  (kaggle.com/datasets/manwithacat/oracc-akkadian-english-parallel-corpus) --
  2,117 pairs from RIAo/RINAP/RIBo/SAAo, a strict subset of the 4 projects
  among the 138 the Zenodo HTML cache already covers at higher volume.
- **EvaCun** (Zenodo 17220688) -- line-aligned 3-column parallel corpus
  (cuneiform/transliteration/English) from ORACC. No tablet-id column at
  all (alignment is by row index only within each file), so it can't be
  joined back onto our tablet_id-keyed schema. Only useful as a wholly
  separate corpus for training a dedicated translation model from scratch,
  not for enriching existing per-tablet records.
- **ETCSL** (Electronic Text Corpus of Sumerian Literature, Oxford) -- 394
  Sumerian literary composite texts with translation. Composites have no
  CDLI P-number linkage to the individual physical tablets/exemplars they
  were assembled from (same structural problem as ORACC's own "Q"-number
  composites), so there's no clean join key. Also the genre it would help
  (Literary & Scholarly) is already the best-covered one (see below) via
  ORACC's own literary sub-projects.

## Final coverage

| granularity | coverage |
|---|---|
| `documents` (56,934 unique tablets) | 17.6% |
| line-level (`default`/`signs_translit`) | ~28-31% |

By genre (documents config):

| genre | coverage |
|---|---|
| Literary & Scholarly | 54.9% |
| Legal | 30.1% |
| Royal Inscriptions | 15.1% |
| Letters | 14.1% |
| Administrative | 9.2% |
| Lexical | 1.8% |

Uneven by design, not a bug -- translation effort in the field goes to
literature/law/royal texts, essentially never to routine administrative
records. Treat 17.6%/30% as the realistic ceiling from open scholarly
sources; searching further is likely to surface only re-packagings of the
same three root sources (CDLI/eBL/ORACC) rather than new coverage.

## Not done yet, worth revisiting

The ORACC HTML pages this session parsed carry genuine per-line structure
(`p.tr` cells 1:1 with transliteration lines) that got flattened into one
whole-document `translation` string, same as `text`/`signs` already are.
A real per-line 3-column view (cuneiform | transliteration | translation)
in the web demo's "Similar documents" card -- discussed, not built --
would reuse this same parsed structure instead of re-deriving it.
`demo_predictions.py`'s `build_line_table()`/`atf_to_lines()` already do
this for CDLI/eBL-sourced showcase tablets; the ORACC HTML path would need
an equivalent per-line extraction (keep line index instead of joining).
