"""Deterministic grounding check: numbers and names an answer states must
appear in what was retrieved.

The compliance LLM (9–18 s) waved through "Calvert-Lewin kam zu spät [4]"
(nobody of that name in any chunk) and "der verfluchte Dienstag (2020) …
6.350 Verletzte" (a 2024 figure, and 2020 nowhere in the pool). Both are
checkable without a model: every figure and every foreign proper name in the
answer must occur somewhere in the question or the retrieved text.

Deliberately narrow, because a false alarm costs a rewrite:
- numbers: language-independent; small counts (< 10) and day-of-month are
  skipped, since answers legitimately count ("drei Vereine") and write dates
  their own way.
- names: only shapes that are names in any language – hyphenated
  capitalised compounds ("Calvert-Lewin") and "Firstname Lastname" pairs of
  non-German-looking words. Plain capitalised words are not checked: German
  capitalises every noun, and against English sources nearly all of them
  would "not be found".
"""

from __future__ import annotations

import re
import unicodedata

_NUMBER_RE = re.compile(r"(?<![\w.,])\d{1,3}(?:[.,  ]\d{3})+(?:[.,]\d+)?(?![\w])|(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])")
_SCALE_RE = re.compile(r"\s*(Millionen|Million|Mio\.?|million|Milliarden|Milliarde|Mrd\.?|billion)\b", re.I)
_SCALES = {"million": 1e6, "millionen": 1e6, "mio": 1e6, "mio.": 1e6,
           "milliarde": 1e9, "milliarden": 1e9, "mrd": 1e9, "mrd.": 1e9, "billion": 1e9}
_CITATION_RE = re.compile(r"\[\d+(?:\s*[,–-]\s*\d+)*\]")
_LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s", re.M)
_MONTHS = ("januar|februar|märz|april|mai|juni|juli|august|september|oktober|november|"
           "dezember|jan|feb|mär|apr|jun|jul|aug|sep|sept|okt|nov|dez")
_DAY_OF_MONTH_RE = re.compile(rf"\b\d{{1,2}}\.\s*(?:{_MONTHS})\b", re.I)
_EN_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
}

# Shapes that are proper names in any language.
_CAP = r"[A-Z][a-zà-öø-ÿ']{2,}"
_HYPHEN_NAME_RE = re.compile(rf"\b{_CAP}(?:-{_CAP})+\b")
_NAME_PAIR_RE = re.compile(rf"\b{_CAP}\s+{_CAP}\b")
_GERMAN_LOOKING = re.compile(
    r"[äöüßÄÖÜ]|(?:ung|heit|keit|schaft|ium|isch|ische[nrms]?|lich|ungen|tät|ismus|"
    r"ierung|ion|ionen|ern|chen|lein|nis|nisse|tum|ei|ien)$"
)
# A capital that is only there because a sentence, bullet or bold run starts.
_LINE_START_RE = re.compile(r"(?:^|[.!?:;]\s+|\n\s*(?:[-*•]|\d+[.)])?\s*|\*\*)$")
_STOPWORDS = {
    "Der", "Die", "Das", "Den", "Dem", "Des", "Ein", "Eine", "Einen", "Einem", "Einer",
    "Im", "In", "Am", "An", "Auf", "Aus", "Bei", "Beim", "Laut", "Zum", "Zur", "Vom",
    "Von", "Mit", "Nach", "Vor", "Für", "Gegen", "Über", "Unter", "Seit", "Bis",
    "Auch", "Zudem", "Dabei", "Damit", "Somit", "Insgesamt", "Allerdings", "Jedoch",
    "Diese", "Dieser", "Dieses", "Welche", "Welcher", "Quelle", "Quellen",
}


def _fold(text: str) -> str:
    """Lowercase, accents removed – "Mbappé" matches "Mbappe"."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)
    )


def _number_values(raw: str, scale_word: str | None) -> set[str]:
    """Normalised forms a number may take in another language's source."""
    digits = re.sub(r"[  ]", "", raw)
    forms: set[str] = set()
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", digits):
        forms.add(re.sub(r"[.,]", "", digits))          # 6.350 / 6,350 → 6350
    else:
        forms.add(digits.replace(",", "."))             # 1,2 → 1.2
        forms.add(re.sub(r"[.,]", "", digits))
    if scale_word:
        try:
            value = float(digits.replace(".", "").replace(",", ".")) if "," in digits else float(digits)
            forms.add(str(int(round(value * _SCALES[scale_word.lower()]))))
        except ValueError:
            pass
    return {f.rstrip(".") for f in forms if f}


def _numbers_in(text: str, *, for_answer: bool) -> list[tuple[str, set[str]]]:
    if for_answer:
        text = _CITATION_RE.sub(" ", text)
        text = _LIST_MARKER_RE.sub(" ", text)
        text = _DAY_OF_MONTH_RE.sub(" ", text)
    out = []
    for m in _NUMBER_RE.finditer(text):
        scale = _SCALE_RE.match(text, m.end())
        out.append((m.group(0), _number_values(m.group(0), scale.group(1) if scale else None)))
    return out


def _source_numbers(text: str) -> set[str]:
    values: set[str] = set()
    for _, forms in _numbers_in(text, for_answer=False):
        values |= forms
    for word, n in _EN_NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", text, re.I):
            values.add(str(n))
    return values


def _is_small_count(forms: set[str]) -> bool:
    try:
        return all(float(f) < 10 for f in forms)
    except ValueError:
        return False


def _looks_like_name(words: list[str]) -> bool:
    return not any(w in _STOPWORDS or _GERMAN_LOOKING.search(w) for w in words)


def _name_candidates(answer: str) -> list[str]:
    """Hyphenated and two-word proper names – but not German word pairs.

    Measured false alarms on 100 answers: "Weitere Angaben", "Folgende
    Prominente", "Beide Preise", "Dritter Platz" (capitalised only because a
    sentence or bullet starts), "Live-Daten", "Saudi-Arabien" (German nouns
    and German country names). Each one cost a needless rewrite.
    """
    names = []
    for m in _HYPHEN_NAME_RE.finditer(answer):
        parts = m.group(0).split("-")
        # A German compound ends in a German noun, often plural: "Live-Daten",
        # "Fan-Zonen". Names rarely do ("Calvert-Lewin", "Alexander-Arnold").
        if _looks_like_name(parts) and not parts[-1].endswith(("en", "e", "er")):
            names.append(m.group(0))
    for m in _NAME_PAIR_RE.finditer(answer):
        words = m.group(0).split()
        if words[0] in _STOPWORDS:
            continue
        at_start = bool(_LINE_START_RE.search(answer[: m.start()]))
        # At a sentence start the first word may be any capitalised German
        # word; only the second one then says whether this is a name.
        if not _looks_like_name(words[1:] if at_start else words):
            continue
        if at_start and words[0].endswith(("e", "er", "es", "en", "em")) and len(words[0]) > 4:
            continue  # "Weitere …", "Folgende …", "Beide …" – an adjective, not a first name
        names.append(m.group(0))
    return list(dict.fromkeys(names))


def _name_grounded(name: str, folded_source: str, source_tokens: set[str]) -> bool:
    if _fold(name) in folded_source:
        return True
    # A part found is enough: "Hisbollah-Kommandeur" vs. "Hezbollah commander"
    # must not count as invented. Prefix match tolerates inflection/spelling
    # ("Hisbollah"/"Hezbollah" share "h…bollah" only loosely, so compare the
    # tail too).
    for part in re.split(r"[-\s]+", _fold(name)):
        if len(part) < 4:
            continue
        if part in source_tokens or any(
            t.startswith(part[:5]) or (len(part) > 6 and t.endswith(part[-5:]))
            for t in source_tokens
        ):
            return True
    return False


def find_ungrounded(answer: str, sources: list[str]) -> dict:
    """Figures and names in ``answer`` that occur in none of ``sources``.

    ``sources`` should be the question plus every retrieved text the answer
    could draw on. Returns ``{"numbers": [...], "names": [...]}`` – the
    surface forms as written in the answer.
    """
    source_text = "\n".join(s for s in sources if s)
    source_values = _source_numbers(source_text)
    folded = _fold(source_text)
    tokens = set(re.findall(r"[a-z0-9']{3,}", folded))

    numbers = [
        raw for raw, forms in _numbers_in(answer, for_answer=True)
        if not _is_small_count(forms) and not (forms & source_values)
    ]
    names = [n for n in _name_candidates(answer) if not _name_grounded(n, folded, tokens)]
    return {"numbers": list(dict.fromkeys(numbers)), "names": names}
