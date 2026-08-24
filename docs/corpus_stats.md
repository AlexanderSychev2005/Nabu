# Статистика корпуса (session 2026-08-24)

Посчитано напрямую из финального датасета (`data/processed/hf_dataset_documents_with_cdli_bulk`,
все три сплита train+validation+test вместе) и `data/processed/hf_dataset_vision`.
Не копировать вручную в другие файлы — при следующем изменении корпуса пересчитать заново
(см. скрипты в конце файла).

## Категории по головам: документы и фото

### Period (9 классов, 55 767 документов с меткой / 9 091 фото с меткой из 9 245)

| Категория | Документов | Фото |
|---|---|---|
| Ur III | 23 527 | 2 413 |
| Old Babylonian | 9 602 | 2 215 |
| Neo-Assyrian | 9 412 | 913 |
| Third Millennium | 4 717 | 1 641 |
| Old Assyrian | 2 569 | 676 |
| Middle Assyrian | 2 552 | 800 |
| Middle Babylonian | 1 856 | 338 |
| Neo-Babylonian | 1 082 | 57 |
| Late Antiquity | 450 | 38 |

### Genre (6 классов, 49 498 документов / 7 881 фото)

| Категория | Документов | Фото |
|---|---|---|
| Administrative | 31 076 | 4 793 |
| Royal Inscriptions | 4 516 | 334 |
| Lexical | 3 868 | 267 |
| Literary & Scholarly | 3 517 | 880 |
| Legal | 3 517 | 802 |
| Letters | 3 004 | 805 |

### Language (4 класса, 45 636 документов / 6 241 фото)

| Категория | Документов | Фото |
|---|---|---|
| Sumerian | 29 077 | 3 619 |
| Akkadian | 15 147 | 2 313 |
| Peripheral/Other | 1 136 | 280 |
| Bilingual | 276 | 29 |

### Provenience (36 классов, 50 386 документов / 8 836 фото)

| Категория | Документов | Фото |
|---|---|---|
| Umma | 11 522 | 672 |
| Nineveh | 7 500 | 682 |
| Puzriš-Dagan | 6 058 | 586 |
| Nippur | 4 284 | 652 |
| Girsu | 4 058 | 630 |
| Assur | 2 527 | 937 |
| Kanesh | 2 459 | 670 |
| Ur | 1 380 | 504 |
| Sippar | 1 615 | 349 |
| Uruk | 962 | 66 |
| Ugarit | 939 | 0 |
| Nimrud | 629 | 177 |
| Babylon | 400 | 62 |
| Ebla | 411 | 242 |
| Kish | 259 | 180 |
| Nuzi | 198 | 186 |
| Irisagrig | 123 | 61 |
| Persepolis | 100 | 34 |
| Ešnunna | 154 | 151 |
| Šaduppum | 140 | 4 |
| Pī-Kasî | 104 | 57 |
| Hattusa | 104 | 6 |
| Susa | 63 | 8 |
| Zabalam | 60 | 54 |
| Isin | 707 | 153 |
| Garšana | 655 | 101 |
| Adab | 603 | 363 |
| Larsa | 521 | 220 |
| Šuruppak | 316 | 297 |
| Kisurra | 374 | 0 |
| Tuttul | 356 | 331 |
| Nerebtum | 203 | 206 |
| Amarna | 198 | 195 |
| Emar | 183 | 0 |
| Huzirina | 109 | 0 |
| Mari | 112 | 0 |

Примечание: Ugarit, Mari, Emar, Kisurra, Huzirina — 0 фото (документы есть, отревьюженных
фотографий по этим находкам пока не набралось; не баг).

## Объём текста в корпусе

Полный нетронутый текст (без обрезки по `context_char_max`), все три сплита вместе.

| Единица | Значение |
|---|---|
| Документов | 56 934 |
| Символов | 19 894 123 (~19.9M) |
| Слов (по пробелам) | 3 007 072 (~3.0M) |
| Документов со знаками (`signs`) | 55 652 |
| Клинописных знаков | 5 700 408 (~5.7M) |
| WordPiece-токенов (mBERT, `bert-base-multilingual-cased`) | 10 907 100 (~10.9M) |

**Для сравнения:**
- Lazar et al. (2021): ~10 000 табличек, 1M слов, 2.3M знаков (ORACC).
- Aeneas / LED (Assael et al. 2025): 176 861 надписей, 16M символов.

У нас корпус примерно в 5.7 раз больше Lazar et al. по числу документов, ~2.5-3x по
объёму текста (слова/знаки). У Aeneas документов больше (176.8k против наших 56.9k), но
у нас больше символов на документ в среднем — латинские надписи короче, наши таблички
часто содержат более длинный текст.

## Как пересчитать

```bash
# категории по головам (docs + photos)
uv run python -c "
import json
from collections import Counter
from datasets import load_from_disk

docs = load_from_disk('data/processed/hf_dataset_documents_with_cdli_bulk')
vis = load_from_disk('data/processed/hf_dataset_vision')
lc = json.load(open('data/processed/label_configs.json', encoding='utf-8'))

for head in ['period', 'genre', 'language', 'provenience']:
    labels = lc[head]['labels']
    doc_counts = Counter()
    for split in docs:
        for idx in docs[split][f'{head}_labels']:
            if idx != -100:
                doc_counts[labels[idx]] += 1
    img_counts = Counter()
    for split in vis:
        for val in vis[split][head]:
            if val in labels:
                img_counts[val] += 1
    print(f'=== {head} ===')
    for cls in labels:
        print(f'  {cls}: docs={doc_counts.get(cls, 0)} photos={img_counts.get(cls, 0)}')
"

# объём текста
uv run python -c "
from datasets import load_from_disk
from transformers import AutoTokenizer

ds = load_from_disk('data/processed/hf_dataset_documents_with_cdli_bulk')
tok = AutoTokenizer.from_pretrained(r'checkpoints_final_text/final_model', use_fast=True)

total_docs = total_chars = total_words = total_signs_docs = total_signs = 0
for split in ds:
    for row in ds[split]:
        text = row['text'] or ''
        total_docs += 1
        total_chars += len(text)
        total_words += len(text.split())
        if row['signs']:
            total_signs_docs += 1
            total_signs += len(row['signs'])
print(f'documents={total_docs} chars={total_chars} words={total_words} signs_docs={total_signs_docs} signs={total_signs}')

wp_total = 0
for split in ds:
    texts = ds[split]['text']
    for i in range(0, len(texts), 2000):
        batch = [t or '' for t in texts[i:i+2000]]
        enc = tok(batch, add_special_tokens=False)
        wp_total += sum(len(ids) for ids in enc['input_ids'])
print(f'wordpiece_tokens={wp_total}')
"
```
