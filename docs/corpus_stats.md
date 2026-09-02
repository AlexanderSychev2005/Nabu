# Статистика корпуса (session 2026-08-31, после len(signs)<2 фикса + showcase-добавлений + доразметки фото)

Посчитано напрямую из финального датасета (`data/processed/hf_dataset_documents_with_cdli_bulk`,
все три сплита train+validation+test вместе) и `data/processed/hf_dataset_vision`.
Не копировать вручную в другие файлы — при следующем изменении корпуса пересчитать заново
(см. скрипты в конце файла).

**Крупные изменения с прошлой версии (session 2026-08-24):** исправлен баг `len(signs) < 2`,
из-за которого строки с реальной транслитерацией, но без распознанных клинописных знаков
(нормализованные издания ORACC вроде CMAwR), целиком выбрасывались — документов стало
56 934 → 126 023 (×2.2). Добавлены showcase-тексты Энхедуанны (гимн "Нин-ме-шара"/Восхваление
Инанны, Храмовые гимны, диск) и стела Хаммурапи целиком. Фото доразмечены (bbox review) —
9 245 → 12 351.

## Категории по головам: документы и фото

### Period (9 классов, 107 865 документов с меткой / 12 186 фото с меткой из 12 351)

| Категория | Документов | Фото |
|---|---|---|
| Ur III | 62 701 | 4 160 |
| Old Babylonian | 12 039 | 2 306 |
| Neo-Assyrian | 11 751 | 1 319 |
| Third Millennium | 11 444 | 2 049 |
| Old Assyrian | 2 583 | 943 |
| Middle Assyrian | 2 589 | 801 |
| Middle Babylonian | 2 046 | 343 |
| Neo-Babylonian | 1 267 | 69 |
| Late Antiquity | 1 445 | 196 |

### Genre (6 классов, 101 119 документов / 10 757 фото)

| Категория | Документов | Фото |
|---|---|---|
| Administrative | 77 088 | 6 950 |
| Legal | 6 261 | 1 054 |
| Royal Inscriptions | 5 257 | 344 |
| Literary & Scholarly | 4 952 | 1 101 |
| Lexical | 4 025 | 281 |
| Letters | 3 536 | 1 027 |

### Language (4 класса, 96 911 документов / 8 789 фото)

| Категория | Документов | Фото |
|---|---|---|
| Sumerian | 76 520 | 5 580 |
| Akkadian | 18 627 | 2 897 |
| Peripheral/Other | 1 211 | 282 |
| Bilingual | 553 | 30 |

### Provenience (36 классов, 98 401 документов / 11 941 фото)

| Категория | Документов | Фото |
|---|---|---|
| Umma | 25 567 | 917 |
| Girsu | 18 746 | 911 |
| Puzriš-Dagan | 10 080 | 843 |
| Nineveh | 7 757 | 927 |
| Nippur | 6 250 | 931 |
| Ur | 4 717 | 796 |
| Assur | 3 338 | 937 |
| Irisagrig | 2 668 | 61 |
| Kanesh | 2 459 | 937 |
| Adab | 2 427 | 762 |
| Uruk | 1 792 | 230 |
| Sippar | 1 743 | 409 |
| Garšana | 1 600 | 510 |
| Nimrud | 1 451 | 312 |
| Ugarit | 1 173 | 1 |
| Isin | 957 | 153 |
| Babylon | 736 | 72 |
| Larsa | 673 | 230 |
| Ebla | 427 | 242 |
| Kisurra | 410 | 0 |
| Zabalam | 370 | 54 |
| Tuttul | 356 | 350 |
| Šuruppak | 318 | 305 |
| Kish | 301 | 203 |
| Ešnunna | 280 | 151 |
| Nuzi | 237 | 186 |
| Susa | 221 | 9 |
| Amarna | 207 | 195 |
| Nerebtum | 205 | 206 |
| Emar | 184 | 0 |
| Mari | 158 | 0 |
| Šaduppum | 144 | 4 |
| Huzirina | 125 | 0 |
| Persepolis | 109 | 34 |
| Pī-Kasî | 110 | 57 |
| Hattusa | 105 | 6 |

Примечание: Ugarit, Mari, Emar, Kisurra, Huzirina — 0 или почти 0 фото несмотря на добор в
этой сессии (`backfill_class_balance_images.py`) — у CDLI просто нет больше фото для этих
классов сверх уже собранного (не баг, физический потолок доступности).

## Объём текста в корпусе

Полный нетронутый текст (без обрезки по `context_char_max`), все три сплита вместе.

| Единица | Значение |
|---|---|
| Документов | 126 023 |
| Символов | 34 497 640 (~34.5M) |
| Слов (по пробелам) | 5 280 007 (~5.3M) |
| Документов со знаками (`signs`) | 55 670 |
| Клинописных знаков | 5 708 213 (~5.7M) |
| WordPiece-токенов (mBERT, `bert-base-multilingual-cased`) | 18 270 899 (~18.3M) |

**Для сравнения:**
- Lazar et al. (2021): ~10 000 табличек, 1M слов, 2.3M знаков (ORACC).
- Aeneas / LED (Assael et al. 2025): 176 861 надписей, 16M символов.

У нас корпус примерно в 12.6 раз больше Lazar et al. по числу документов, ~5x по объёму
текста (слова/знаки). Aeneas всё ещё больше по числу отдельных надписей (176.8k против
наших 126k), но по объёму текста мы его уже обошли (34.5M символов против 16M) — латинские
надписи короче, а наши таблички после фикса содержат существенно больше текста на документ
в среднем, особенно за счёт крупных составных изданий вроде стелы Хаммурапи (38.8k символов
в одном документе).

Число документов со знаками (55 670) почти не изменилось относительно прошлой версии
(55 652) — фикс не добавил новых знаков, он перестал выбрасывать текст у документов, где
знаков и раньше не было (нормализованные ORACC-издания). Весь прирост в 69 089 документов —
это текст без знаковой разметки.

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
