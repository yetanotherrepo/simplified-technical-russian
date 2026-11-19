#!/usr/bin/env python3
"""Advisory Russian prose checks. No rewriting, network or semantic verdict."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys


VERSION = "0.2.1"
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
TOKEN = re.compile(r"[+-]?\d+(?:[.,_]\d+)*|[^\W_]+(?:[-‑][^\W_]+)*", re.UNICODE)
# Deliberately a Markdown subset, not a full parser. See references/checking.md.
INLINE = re.compile(
    r"(?P<ticks>`+)(?!`)[^\n]*?(?P=ticks)(?!`)"
    r'|«[^»\n]*»|“[^”\n]*”|"[^"\n]*"'
    r"|https?://[^\s<>\"«»`]+"
    r"|(?<!\w)[A-Z][A-Z0-9]+-\d+(?!\w)"
    r"|(?<!\w)(?:/|\./|\.\./)[\w./-]+"
)
MARKERS = {
    "NEGATION": r"\b(?:не|нет|нельзя|запрещено|запрещается|без)\b",
    "OBLIGATION": r"\b(?:необходимо|обязательно|должен|должна|должно|должны|требуется)\b",
    "PERMISSION": r"\b(?:можно|разрешено|разрешается|допускается)\b",
    "RECOMMENDATION": r"\b(?:рекомендуется|рекомендуем|желательно)\b",
    "CONDITION": r"\b(?:если|при условии)\b",
    "RESTRICTION": r"\b(?:только|лишь|исключительно)\b",
    "EXCEPTION": r"\b(?:кроме|за исключением)\b",
    "SIMULTANEOUS": r"\b(?:одновременно|удерживая)\b",
    "BOUND": r"\b(?:не более|не менее|более|менее|как минимум|как максимум)\b",
}
QUANTITY = re.compile(
    r"(?<!\w)([+-]?\d+(?:[.,]\d+)*)\s*"
    r"(мс|миллисекунд\w*|секунд\w*|сек|с|минут\w*|мин|час\w*|ч|"
    r"кб|мб|гб|тб|байт\w*|мм|см|км|м|кг|г|%|°c)(?!\w)", re.I
)


def normalize(value):
    return value.casefold().replace("ё", "е")


def protected_spans(text):
    """Return disjoint literal spans; fenced blocks may be unclosed."""
    spans = []
    offset = 0
    opened = None
    start = 0
    for line in text.splitlines(keepends=True):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if opened:
            if fence and fence[1][0] == opened[0] and len(fence[1]) >= len(opened) and not fence[2].strip():
                spans.append((start, offset + len(line), text[start:offset + len(line)]))
                opened = None
        elif fence:
            opened, start = fence[1], offset
        else:
            for match in INLINE.finditer(line):
                a, b = match.span()
                # Sentence punctuation is not part of a bare URL/path.
                if match[0].startswith(("http://", "https://", "/", "./", "../")):
                    b = a + len(match[0].rstrip(".,;:!?)]}"))
                spans.append((offset + a, offset + b, line[a:b]))
        offset += len(line)
    if opened:
        spans.append((start, len(text), text[start:]))
    return spans


def mask_literals(text, count_as_word=False):
    chars = list(text)
    for start, end, _ in protected_spans(text):
        for i in range(start, end):
            if chars[i] not in "\r\n":
                chars[i] = " "
        if count_as_word and start < end:
            chars[start] = "X"
    return "".join(chars)


def finding(code, line, message, basis):
    return {"code": code, "level": "advice", "line": line, "message": message, "basis": basis}


def phrase_pattern(phrase):
    return re.compile(r"(?<!\w)" + re.escape(normalize(phrase)) + r"(?!\w)")


def validate_glossary(data):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Глоссарий: требуется schema_version: 1.")
    if set(data) != {"schema_version", "entries"} or not isinstance(data["entries"], list):
        raise ValueError("Глоссарий: ожидаются только schema_version и список entries.")
    seen = set()
    for entry in data["entries"]:
        required = {"term", "definition", "variants", "status", "source"}
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("Запись глоссария: нужны term, definition, variants, status, source.")
        for key in ("term", "definition", "source"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValueError("Запись глоссария: текстовые поля не должны быть пустыми.")
        if entry["status"] not in ("proposed", "approved"):
            raise ValueError("Статус термина: proposed или approved.")
        if not isinstance(entry["variants"], list) or any(not isinstance(x, str) or not x.strip() for x in entry["variants"]):
            raise ValueError("variants: требуется список непустых строк.")
        for form in [entry["term"]] + entry["variants"]:
            key = normalize(form)
            if key in seen:
                raise ValueError("Глоссарий: повторяющаяся или конфликтующая форма термина.")
            seen.add(key)
    return data["entries"]


def style_findings(text, glossary=None):
    findings = []
    prose = mask_literals(text, count_as_word=True)
    prose = re.sub(r"(?m)^ {0,3}#{1,6}[ \t]+[^\n]*", lambda m: " " * len(m[0]), prose)
    # Paragraph breaks, headings and individual list items delimit prose blocks.
    blocks = re.split(r"\n\s*\n|(?m:^)(?= {0,3}(?:#{1,6}\s|[-*+]\s|\d+[.)]\s))", prose)
    offset = 0
    for block in blocks:
        start = prose.find(block, offset)
        offset = start + len(block)
        if not CYRILLIC.search(block):
            continue
        # Parenthetical text is considered a separate sentence. Offsets are retained.
        sentences = list(re.finditer(r"[^.!?;()]+", re.sub(r"(?<=\d)[.,](?=\d)", "_", block)))
        meaningful = [m for m in sentences if TOKEN.search(m[0])]
        if len(meaningful) > 6:
            findings.append(finding("PARAGRAPH_LENGTH", text.count("\n", 0, start) + 1,
                                    "В абзаце примерно больше 6 предложений; проверьте деление по темам.", "ГОСТ 8.2.6.2; приблизительно"))
        for match in meaningful:
            words = TOKEN.findall(match[0])
            if len(words) > 20 and CYRILLIC.search(match[0]):
                pos = start + match.start() + len(match[0]) - len(match[0].lstrip())
                findings.append(finding("SENTENCE_LENGTH", text.count("\n", 0, pos) + 1,
                                        "Около %s слов; ориентир — 20. Сохраните условия при сокращении." % len(words),
                                        "ГОСТ 8.2.5.1, 8.2.6.1; приблизительно"))
    visible = normalize(mask_literals(text))
    for phrase in ("осуществить выполнение", "произвести проверку", "в целях осуществления"):
        for match in phrase_pattern(phrase).finditer(visible):
            findings.append(finding("VERBOSE_PHRASE", text.count("\n", 0, match.start()) + 1,
                                    "Проверьте, можно ли заменить оборот прямым глаголом без потери смысла.",
                                    "ГОСТ 8.2.3.10–8.2.3.14; эвристика скилла"))
    for entry in glossary or []:
        for variant in entry["variants"]:
            for match in phrase_pattern(variant).finditer(visible):
                status = "рабочее предложение" if entry["status"] == "proposed" else "статус approved указан в глоссарии"
                findings.append(finding("GLOSSARY_VARIANT", text.count("\n", 0, match.start()) + 1,
                                        "Сверьте термин с «%s» (%s); автоматической замены нет." % (entry["term"], status),
                                        "Глоссарий проекта; ГОСТ 8.2.3.7"))
    return findings


def restriction_targets(text):
    """Count short lexical anchors after restrictive particles, ignoring clause order."""
    # Keep literals atomic so a change from 'only `a`' to 'only `b`' is visible
    # even when both literals still appear elsewhere. Keep their original case.
    tokens = []
    def add_prose(fragment):
        # For lexical anchors, skip emphasis delimiters at word edges.
        # Avoid scanning the rest of the paragraph for a closing delimiter.
        fragment = re.sub(r"(?<!\w)_{1,3}(?=[^\W_])|(?<=[^\W_])_{1,3}(?!\w)", "", fragment)
        fragment = fragment.replace("*", "")
        words = re.findall(r"\w+(?:[-‑]\w+)*|[^\w\s]", normalize(fragment))
        tokens.extend(word for word in words if word not in ("_", "__", "___"))

    end = 0
    for start, stop, literal in protected_spans(text):
        add_prose(text[end:start])
        tokens.append(("literal", literal))
        end = stop
    add_prose(text[end:])

    targets = Counter()
    for index, token in enumerate(tokens):
        if token not in ("только", "лишь", "исключительно"):
            continue
        if index and tokens[index - 1] in ("не", "как"):
            continue
        following = tokens[index + 1:index + 3]
        if not following:
            continue
        first = following[0]
        # Qualifiers such as 'временную' alone cannot distinguish two objects.
        qualifier = isinstance(first, str) and (
            first in ("в", "на", "по", "для", "после", "до", "при", "из", "с", "к")
            or first.endswith(("ый", "ий", "ой", "ая", "яя", "ую", "юю", "ое", "ее",
                               "ые", "ие", "ых", "их", "ого", "его", "ом", "ем"))
        )
        targets[tuple(following if qualifier else following[:1])] += 1
    return targets


def compare_findings(before, after):
    findings = []
    def changed(code, label, left, right):
        if left != right:
            findings.append(finding(code, None, label + ": состав или количество изменились; сверьте исходник и результат.",
                                    "Защита смысла в скилле; поверхностный сигнал, не доказательство ошибки"))
    changed("LITERAL_CHANGED", "Технические литералы или цитаты",
            Counter(x[2] for x in protected_spans(before)), Counter(x[2] for x in protected_spans(after)))
    changed("RESTRICTION_SCOPE_CHANGED", "Слова после ограничительных частиц",
            restriction_targets(before), restriction_targets(after))
    clean = []
    for text in (before, after):
        value = normalize(mask_literals(text))
        value = re.sub(r"(?m)^\s*\d+[.)]\s+", "", value)
        clean.append(value)
    left, right = clean
    number_pattern = r"(?<!\w)[+-]?\d+(?:[.,]\d+)*(?!\w)"
    changed("NUMBER_CHANGED", "Числа", Counter(re.findall(number_pattern, left)), Counter(re.findall(number_pattern, right)))
    changed("QUANTITY_CHANGED", "Величины и единицы", Counter(QUANTITY.findall(left)), Counter(QUANTITY.findall(right)))
    for name, pattern in MARKERS.items():
        left_matches, right_matches = re.findall(pattern, left), re.findall(pattern, right)
        changed(name + "_CHANGED", "Маркеры: " + name,
                Counter(left_matches) if name == "BOUND" else len(left_matches),
                Counter(right_matches) if name == "BOUND" else len(right_matches))
    return findings


def report(text, before=None, glossary=None):
    findings = style_findings(text, glossary)
    if before is not None:
        findings.extend(compare_findings(before, text))
    return {"version": VERSION, "findings": findings,
            "comparison_performed": before is not None, "semantic_equivalence": "not_assessed",
            "notice": "Рекомендательная проверка. Отсутствие замечаний не подтверждает сохранение смысла или соответствие ГОСТ."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Рекомендательный линтер УТР. Python 3.9+, без внешних зависимостей.")
    parser.add_argument("file", help="UTF-8 текст/Markdown или - для stdin")
    parser.add_argument("--before", help="Исходный UTF-8 файл для сравнения с результатом")
    parser.add_argument("--glossary", help="JSON-глоссарий по assets/glossary.template.json")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--fail-on", choices=("never", "advice"), default="never", help="По умолчанию замечания не меняют код выхода")
    args = parser.parse_args(argv)
    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig")
        before = Path(args.before).read_text(encoding="utf-8-sig") if args.before else None
        glossary = validate_glossary(json.loads(Path(args.glossary).read_text(encoding="utf-8-sig"))) if args.glossary else None
        result = report(text, before, glossary)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result["findings"]:
            print("%s:%s: %s — %s" % (args.file, item["line"] or "сравнение", item["code"], item["message"]))
        print(result["notice"])
    return 1 if args.fail_on == "advice" and result["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
