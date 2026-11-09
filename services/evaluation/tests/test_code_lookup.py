"""Code lookup: codes in a question are searched verbatim and decide what happens next."""

import pytest
from core import code_lookup
from core.code_lookup import Place, ask_text, extract_terms


def _place(name: str, page: int, n_chunks: int = 1, term: str = "A30042") -> Place:
    chunks = [
        {"chunk_id": f"{name}-{page}-{i}", "score": 1.0, "text": f"… {term} …",
         "metadata": {"file_name": name, "page": page}, "_query_index": 0}
        for i in range(n_chunks)
    ]
    return Place(file_name=name, page=page, terms=[term], snippet=f"… {term} …", chunks=chunks)


def test_extracts_warn_codes_and_parameters_once_in_order():
    assert extract_terms("Warncode A30042, p0251 zurücksetzen", "nochmal A30042") == ["A30042", "p0251"]


def test_ignores_plain_words_and_short_numbers():
    assert extract_terms("Lüfter 2026 Schälanlage p12") == []


def test_ask_text_lists_places_with_counts_and_snippets():
    text = ask_text(["A30042"], [_place("S110/FH3.pdf", 203, 2), _place("S110/LH7.pdf", 1138)])
    assert "A30042 kommt an 2 Stellen in 2 Dokumenten vor" in text
    assert "1. FH3.pdf S. 203 (2×)" in text and "2. LH7.pdf S. 1138 (1×)" in text
    assert "Stichwort" in text


@pytest.fixture()
def places(monkeypatch):
    """Let the test choose what the verbatim search finds."""
    found: list[Place] = []

    async def fake_find(use_case, terms):
        return list(found)

    monkeypatch.setattr(code_lookup, "find_places", fake_find)
    return found


@pytest.mark.anyio
async def test_question_without_a_code_is_left_alone(places):
    assert await code_lookup.lookup("Wie wechsle ich den Lüfter?", "neuman") is None


@pytest.mark.anyio
async def test_a_few_places_are_passed_on_as_candidates(places):
    places += [_place("FH3.pdf", 203), _place("LH7.pdf", 1138)]
    result = await code_lookup.lookup("Warncode A30042", "neuman")
    assert result.ask == "" and not result.narrowed
    assert len(result.pinned) == 2


@pytest.mark.anyio
async def test_a_code_found_nowhere_pins_nothing_and_asks_nothing(places):
    result = await code_lookup.lookup("Warncode A30042", "neuman")
    assert result.places == [] and result.pinned == [] and result.ask == ""


@pytest.mark.anyio
async def test_many_places_are_narrowed_by_the_question(places, monkeypatch):
    places += [_place("A.pdf", i) for i in range(6)]

    async def pick(question, found, use_case):
        return [found[2]]

    monkeypatch.setattr(code_lookup, "_narrow", pick)
    result = await code_lookup.lookup("Warncode A30042 Lüfter", "neuman")
    assert result.narrowed and result.ask == ""
    assert [c["chunk_id"] for c in result.pinned] == ["A.pdf-2-0"]


@pytest.mark.anyio
async def test_many_places_without_a_clear_pick_ask_the_user(places, monkeypatch):
    places += [_place("A.pdf", i) for i in range(6)]

    async def undecided(question, found, use_case):
        return None

    monkeypatch.setattr(code_lookup, "_narrow", undecided)
    result = await code_lookup.lookup("Warncode A30042", "neuman")
    assert result.pinned == []
    assert "kommt an 6 Stellen" in result.ask


@pytest.mark.anyio
async def test_a_widespread_term_is_left_to_the_normal_search(places, monkeypatch):
    places += [_place("A.pdf", i, term="r0949") for i in range(40)]

    async def never(question, found, use_case):
        raise AssertionError("must not narrow a widespread term")

    monkeypatch.setattr(code_lookup, "_narrow", never)
    result = await code_lookup.lookup("Was bedeutet r0949?", "neuman")
    assert result.too_common and result.pinned == [] and result.ask == ""
    assert "zu häufig" in result.describe()


@pytest.mark.anyio
async def test_earlier_messages_supply_the_code_for_a_follow_up(places):
    places += [_place("FH3.pdf", 203)]
    result = await code_lookup.lookup("Lüfter", "neuman", context=["Warncode A30042 am FU"])
    assert result.terms == ["A30042"] and len(result.pinned) == 1


@pytest.mark.anyio
async def test_a_failing_search_never_breaks_the_pipeline(monkeypatch):
    async def broken(use_case, terms):
        raise RuntimeError("vectordb down")

    monkeypatch.setattr(code_lookup, "find_places", broken)
    assert await code_lookup.lookup("Warncode A30042", "neuman") is None
