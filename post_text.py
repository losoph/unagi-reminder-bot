"""Общий модуль разбора и очистки текста поста для Ленты и Telegraph-дайджеста.

Чистые функции без сетевого кода, БД и зависимостей от feed/telegraph_publisher.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_SENTENCE_END_RE = re.compile(r"[.!?…]+(?=\s|$)")
_ABBREVIATIONS = {"г", "руб", "коп", "ул", "д", "стр", "корп", "кв", "т", "см", "мм", "км", "кг", "шт"}

_EMOJI_CLUSTER_RE = re.compile(
    r"((?:[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50-\u2b55](?:[\ufe00-\ufe0f]|[\U0001f3fb-\U0001f3ff]|\u200d[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50-\u2b55])*))(?:\s*\1){2,}"
)

_KNOWN_ABBREVIATIONS = {
    "ИИ", "США", "РФ", "ЕС", "ООН", "НАТО", "АЭС", "СМИ", "МИД", "РБК",
    "ТАСС", "ОАЭ", "ЦРУ", "ФБР", "МЭА", "КОВИД", "COVID", "AI", "GPT",
    "LLM", "API", "CEO", "UI", "SEO", "SMM", "СВО", "СНГ", "РАН", "ВОЗ",
    "РЖД", "МЧС", "ВТО", "МОК", "МВФ", "ЕАЭС", "ОДКБ", "СФР", "ФНС", "ФАС",
    "ВЦИОМ", "ГИБДД", "ТВЭЛ", "ИТЭР", "ТВ", "ЧП", "ДТП", "ИТ", "IT", "VK", "RU",
    "FMCG", "WSJ", "CNN", "BBC", "NASA", "FT", "ПВО", "ВПК", "НПЗ", "ЦБ",
    "БПЛА", "ВВП", "МТС", "ЦОД", "ГБ", "МБ", "ТБ", "ВСУ", "ФСБ", "МВД",
    "КНДР",
}
_VOWELS = set("аеёиоуыэюяaeiouy")

_FORWARDING_PREFIXES_RE = re.compile(
    r"^(?:переслано\s+из\b|forwarded\s+from\b|источник\s*:|via\b|repost\s+from\b).*$",
    re.IGNORECASE,
)
_EMOJI_CHAR_PATTERN = r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50-\u2b55]"
_EMOJI_START_RE = re.compile(rf"^[.,;:!?\s]*{_EMOJI_CHAR_PATTERN}")
_EMOJI_END_RE = re.compile(rf"{_EMOJI_CHAR_PATTERN}[\ufe00-\ufe0f]?[.,;:!?\s]*$")

# Висячие слова (предлоги, союзы, частицы), которые не должны оставаться перед «…»
_HANGING_WORDS = {
    # Русские предлоги
    "в", "во", "на", "по", "с", "со", "к", "ко", "о", "об", "обо", "у", "от", "ото",
    "до", "из", "изо", "за", "при", "про", "под", "подо", "над", "надо", "без", "безо",
    "для", "сквозь", "через", "между", "около", "перед", "передо", "против", "из-за", "из-под",
    "ради", "кроме", "среди", "вслед", "путём", "путем",
    # Русские союзы и частицы
    "и", "а", "но", "да", "или", "либо", "что", "чтобы", "чтоб", "как", "если", "хотя",
    "когда", "пока", "то", "так", "не", "ни", "бы", "ли", "же", "уже", "еще", "ещё", "ведь",
    "тоже", "также",
    # Английские предлоги, союзы, артикли
    "in", "on", "at", "to", "for", "of", "by", "with", "and", "or", "but", "a", "an", "the",
    "as", "if", "from", "into", "onto", "via",
}


def _collapse_emoji_chains(text: str) -> str:
    return _EMOJI_CLUSTER_RE.sub(r"\1", text)


def _clean_service_lines(text: str) -> str:
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        s = line.strip()
        if not s:
            cleaned_lines.append("")
            continue
        without = re.sub(
            r"(@[\w_]+|#[\w_]+|[|·•—\-/,;:\s]|(?:[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50-\u2b55]\ufe0f?))+",
            "",
            s,
        )
        if not without:
            continue
        cleaned_lines.append(line)
    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()
    res = "\n".join(cleaned_lines)
    return re.sub(r"(\s*[@#][\w_]+(\s*[|·•—\-/,;]\s*[@#][\w_]+)*\s*)+$", "", res).rstrip()


def _clean_post_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    text = _collapse_emoji_chains(raw_text)
    text = _clean_service_lines(text)
    return text.strip()


def _strip_forwarding_lines(text: str) -> str:
    """Отбрасывает служебные строки пересылки («Переслано из …» и т.п.), если есть содержательный текст."""
    lines = text.split("\n")
    drop_count = 0
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if _FORWARDING_PREFIXES_RE.match(s):
            drop_count = idx + 1
        else:
            break
    if drop_count > 0:
        remaining = "\n".join(lines[drop_count:]).strip()
        if remaining:
            return remaining
    return text


def _is_banner_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    words = [w for w in s.split() if any(c.isalnum() for c in w)]
    if len(words) == 0 or len(words) > 4:
        return False
    ends_with_excl = s.endswith("!") or bool(re.search(r"!\s*$", s))
    has_emoji_edges = bool(_EMOJI_START_RE.search(s)) and bool(_EMOJI_END_RE.search(s))
    return has_emoji_edges or ends_with_excl


def _strip_banner_line(text: str) -> str:
    """Отбрасывает баннерную первую строку (<=4 слов, эмодзи по краям или «!»), если за ней есть текст."""
    if "\n" not in text:
        return text
    first_line, rest = text.split("\n", 1)
    if not _is_banner_line(first_line):
        return text
    rest_cleaned = _clean_post_text(rest)
    if not rest_cleaned or not any(c.isalpha() for c in rest_cleaned):
        return text
    return rest_cleaned


def clean_post_for_render(text: str) -> str:
    """Полная очистка текста поста для отображения в Ленте и дайджесте."""
    clean = _clean_post_text(text)
    clean = _strip_forwarding_lines(clean)
    clean = _clean_post_text(clean)
    clean = _strip_banner_line(clean)
    clean = _clean_post_text(clean)
    return clean


_clean_post_for_digest = clean_post_for_render


def _extract_bare_url_domain(text: str) -> str | None:
    """Если текст состоит только из ссылки http(s)://..., возвращает домен без www."""
    s = text.strip()
    if not re.fullmatch(r"https?://\S+", s, re.IGNORECASE):
        return None
    try:
        parsed = urlsplit(s)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host if host else None
    except Exception:
        return None


def _is_abbreviation(word: str) -> bool:
    clean_w = re.sub(r"[^\w]", "", word)
    if not clean_w:
        return False
    if clean_w in _KNOWN_ABBREVIATIONS:
        return True
    if len(clean_w) <= 5:
        if re.fullmatch(r"[IVXLCDM]+", clean_w):
            return True
        alpha_chars = [c.lower() for c in clean_w if c.isalpha()]
        if len(alpha_chars) >= 2 and not any(c in _VOWELS for c in alpha_chars):
            return True
    return False


def _normalize_caps_title(title: str) -> str:
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 6:
        return title
    if sum(1 for c in letters if c.isupper()) / len(letters) < 0.7:
        return title

    tokens = re.split(r"(\b[^\W\d_]+\b)", title)
    first_letter_found = False
    new_tokens = []
    for token in tokens:
        if token.isalpha():
            if _is_abbreviation(token):
                new_tokens.append(token)
                first_letter_found = True
            elif not first_letter_found:
                new_tokens.append(token.capitalize())
                first_letter_found = True
            else:
                new_tokens.append(token.lower())
        else:
            new_tokens.append(token)
            if re.search(r"[.!?]\s*$", token):
                first_letter_found = False
    return "".join(new_tokens)


def _trim_trailing_hanging(text: str) -> str:
    """Удаляет висячие предлоги, союзы и знаки препинания с конца строки перед обрезкой."""
    while text:
        text = text.rstrip(" ,;:—–-")
        words = text.split()
        if not words:
            break
        last_word = words[-1].lower().strip("«»\"'()[]{}.,;:—–-")
        if last_word in _HANGING_WORDS:
            text = " ".join(words[:-1]).rstrip()
        else:
            break
    return text.rstrip(" ,;:—–-")


def _truncate_by_word(text: str, limit: int) -> str:
    """Обрезает строку по границе слова с многоточием, не оставляя висячих предлогов/союзов."""
    text = " ".join(text.split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut_limit = max(0, limit - 1)
    last_space = text[:cut_limit].rfind(" ")
    if last_space != -1:
        candidate = text[:last_space].rstrip()
    else:
        candidate = text[:cut_limit]

    cleaned = _trim_trailing_hanging(candidate)
    if not cleaned:
        cleaned = candidate.rstrip(" ,;:—–-")
    return cleaned + "…"


def _find_sentence_boundary(text: str) -> int | None:
    for m in _SENTENCE_END_RE.finditer(text):
        pos = m.start()
        prefix = text[:pos].rstrip()
        words = prefix.split()
        if not words:
            continue
        token = words[-1]
        if len(words) == 1 and token.isdigit():
            continue
        if token.lower() in _ABBREVIATIONS:
            continue
        return m.end()
    return None


def _split_sentences(text: str) -> list[str]:
    sentences = []
    current = " ".join(text.split())
    while current:
        b = _find_sentence_boundary(current)
        if b is not None:
            sentences.append(current[:b].strip())
            current = current[b:].strip()
        else:
            sentences.append(current.strip())
            break
    return sentences


def _find_semantic_cut(text: str, min_len: int = 50, max_len: int = 140) -> str | None:
    # Приоритет разделителей смысловых частей: ":", " — ", ", "
    for delim in (r":(?:\s|$)", r"\s+[—–]\s+", r",\s+"):
        candidates = []
        for m in re.finditer(delim, text):
            prefix = text[:m.start()].strip()
            clean_p = " ".join(prefix.split())
            clean_p = _trim_trailing_hanging(clean_p)
            if min_len <= len(clean_p) <= max_len:
                candidates.append(clean_p)
        if candidates:
            return candidates[-1]
    return None


def _strip_title_prefix(title: str, essence: str) -> str:
    """Если «суть» начинается теми же словами, что и заголовок, оставляем продолжение (без дубля начала)."""
    base = " ".join(title.rstrip("…").split()).casefold()
    flat = " ".join(essence.split())
    if base and flat.casefold().startswith(base):
        rest = flat[len(base):].lstrip(" ,;:—–-").strip()
        if len(rest) < 20:
            return ""
        rest = rest.lstrip("…. ").strip()
        if not rest:
            return ""
        if rest[:1].islower():
            rest = rest[0].upper() + rest[1:]
        return rest
    flat_clean = flat.lstrip("…. ").strip()
    if flat_clean and flat_clean[:1].islower():
        flat_clean = flat_clean[0].upper() + flat_clean[1:]
    return flat_clean if flat_clean else essence


def _extract_title_and_essence(clean_text: str, max_title: int = 140, max_essence: int = 300) -> tuple[str, str]:
    if not clean_text:
        return "", ""

    newline_pos = clean_text.find("\n")
    punct_pos = _find_sentence_boundary(clean_text)
    if newline_pos != -1 and punct_pos is not None:
        first_end = min(newline_pos, punct_pos)
    elif newline_pos != -1:
        first_end = newline_pos
    elif punct_pos is not None:
        first_end = punct_pos
    else:
        first_end = len(clean_text)

    candidate = clean_text[:first_end].strip()
    first_sentence = " ".join(candidate.split())
    if first_sentence.endswith(".") and not first_sentence.endswith("..") and not first_sentence.endswith("…"):
        first_sentence = first_sentence[:-1].rstrip()

    if len(first_sentence) <= max_title and first_sentence:
        title = _normalize_caps_title(first_sentence)

        rest = clean_text[first_end:].strip()
        if not rest:
            return title, ""
        sentences = _split_sentences(rest)
        if not sentences:
            return title, ""

        s1 = sentences[0]
        if len(sentences) >= 2:
            s2 = sentences[1]
            if len(s1) + 1 + len(s2) <= max_essence:
                essence = f"{s1} {s2}"
            elif len(s1) <= max_essence:
                essence = s1
            else:
                essence = _truncate_by_word(s1, max_essence)
        else:
            if len(s1) <= max_essence:
                essence = s1
            else:
                essence = _truncate_by_word(s1, max_essence)

        title = title.lstrip("…. ").strip()
        if title and title[:1].islower():
            title = title[0].upper() + title[1:]
        essence = essence.lstrip("…. ").strip()
        if essence and essence[:1].islower():
            essence = essence[0].upper() + essence[1:]
        return title, essence

    # Первое предложение > max_title: ищем смысловую часть до разделителя либо режем по слову
    target = first_sentence or " ".join(clean_text.split())
    cut = _find_semantic_cut(target, 50, max_title)
    if cut:
        title = cut
        if title.endswith(".") and not title.endswith("..") and not title.endswith("…"):
            title = title[:-1].rstrip()
    else:
        title = _truncate_by_word(target, max_title)
    title = _normalize_caps_title(title)
    title = title.lstrip("…. ").strip()
    if title and title[:1].islower():
        title = title[0].upper() + title[1:]

    # Если заголовок обрезан: суть = полный исходный текст с начала до max_essence по границе предложения
    sentences = _split_sentences(clean_text)
    if not sentences:
        return title, ""
    if len(sentences[0]) >= 6 and not _is_abbreviation(sentences[0]):
        letters = [c for c in sentences[0] if c.isalpha()]
        if len(letters) >= 6 and sum(1 for c in letters if c.isupper()) / len(letters) >= 0.7:
            sentences[0] = _normalize_caps_title(sentences[0])

    s1 = sentences[0]
    if len(sentences) >= 2:
        s2 = sentences[1]
        if len(s1) + 1 + len(s2) <= max_essence:
            essence = f"{s1} {s2}"
        elif len(s1) <= max_essence:
            essence = s1
        else:
            essence = _truncate_by_word(s1, max_essence)
    else:
        if len(s1) <= max_essence:
            essence = s1
        else:
            essence = _truncate_by_word(s1, max_essence)

    essence = _strip_title_prefix(title, essence)
    essence = essence.lstrip("…. ").strip()
    if essence and essence[:1].islower():
        essence = essence[0].upper() + essence[1:]
    return title, essence


def _first_sentence(text: str) -> tuple[str, str]:
    """Совместимость с feed.py: (первое предложение, остаток)."""
    for index, char in enumerate(text):
        if char in ".!?…" and (index + 1 == len(text) or text[index + 1].isspace()):
            return text[: index + 1].strip(), text[index + 1 :].strip()
    return text, ""


def _split_first_sentence(text: str) -> tuple[str, str]:
    """Отделяет первое предложение (до точки/знака конца или перевода строки)."""
    if not text:
        return "", ""
    newline_idx = text.find("\n")
    match = _SENTENCE_END_RE.search(text)
    end = match.end() if match else None
    if newline_idx != -1 and (end is None or newline_idx < end):
        end = newline_idx
    if end is None:
        return text, ""
    return text[:end].strip(), text[end:].strip()


# Публичные алиасы
clean_post_text = _clean_post_text
extract_title_and_essence = _extract_title_and_essence
normalize_caps_title = _normalize_caps_title
truncate_by_word = _truncate_by_word
extract_bare_url_domain = _extract_bare_url_domain
strip_forwarding_lines = _strip_forwarding_lines
strip_banner_line = _strip_banner_line
is_banner_line = _is_banner_line
split_first_sentence = _split_first_sentence
first_sentence = _first_sentence
strip_title_prefix = _strip_title_prefix
