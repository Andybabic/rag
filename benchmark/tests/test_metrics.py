import metrics
import pytest
import xrag
from report import aggregate, paired_gap


def test_strip_citations():
    assert metrics.strip_citations("Am 2. Oktober [1] und [2, 3].") == "Am 2. Oktober und ."


def test_numbers_match_across_languages():
    assert metrics.numbers("über 14.000 Fälle, 2,5 %") == {"14000", "2.5"}
    assert metrics.numbers("over 14,000 cases, 2.5 %") == {"14000", "2.5"}


def test_number_recall():
    gold = "Der Kongo meldete 14.000 Fälle, Deutschland 3.800."
    assert metrics.number_recall("Kongo: 14,000 [1].", gold) == pytest.approx(0.5)
    assert metrics.number_recall("irgendwas", "keine Zahlen") is None


def test_token_overlap_measures_extra_text():
    gold = "Die Sonnenfinsternis findet zuerst statt."
    short = metrics.token_overlap("Die Sonnenfinsternis findet zuerst statt [1].", gold)
    long = metrics.token_overlap(
        "Die Sonnenfinsternis findet zuerst statt. Außerdem gibt es Meteorschauer, "
        "Planetenkonstellationen und einen zweiten Kometen im November.",
        gold,
    )
    assert short["precision"] == pytest.approx(1.0)
    assert short["recall"] == pytest.approx(1.0)
    assert long["recall"] == pytest.approx(1.0)
    assert long["precision"] < 0.5


def test_length_ratio_ignores_citations():
    assert metrics.length_ratio("eins zwei [1]", "eins zwei") == pytest.approx(1.0)


def test_detect_language():
    assert (
        metrics.detect_language("Die Finsternis ist am 2. Oktober und der Komet kommt danach.")
        == "de"
    )
    assert metrics.detect_language("The eclipse is on October 2 and the comet follows.") == "en"


def test_refusal_detection():
    assert metrics.looks_like_refusal("Dazu liegen keine Informationen in den Dokumenten vor.")
    assert metrics.looks_like_refusal("Text", "REFUSE")
    assert not metrics.looks_like_refusal("Die Finsternis kommt zuerst [1].")


def test_retrieval_metrics():
    support = {"a.md": 2, "b.md": 1, "c.md": 0}
    m = metrics.retrieval_metrics(["c.md", "a.md", "a.md", "x.md", "b.md"], support)
    assert m["recall@1"] == 0.0
    assert m["recall@3"] == pytest.approx(0.5)  # unique docs: c, a, x
    assert m["recall_all"] == 1.0
    assert m["mrr"] == pytest.approx(0.5)
    assert m["sufficient_found"] is True


def test_citation_metrics():
    support = {"a.md": 2, "b.md": 1, "c.md": 0}
    m = metrics.citation_metrics(["a.md", "c.md", "a.md"], support)
    assert m["citation_precision"] == pytest.approx(0.5)
    assert m["citation_recall"] == pytest.approx(0.5)
    assert m["distractor_share"] == pytest.approx(0.5)
    assert metrics.citation_metrics([], support)["citation_recall"] == 0.0


def _row(i, task, urls, support=(2, 1, 0)):
    return {
        "id": i,
        "task": task,
        "question": f"Frage {i}?",
        "answer": f"Antwort {i}",
        "en_question": f"Question {i}?",
        "en_answer": f"Answer {i}",
        "q_type": "comparison",
        "crossdoc_type": 1,
        "urls": list(urls),
        "articles": [f"Text {u}" for u in urls],
        "dates": ["2024-10-01"] * len(urls),
        "is_support": list(support),
        "article_order": list(range(len(urls))),
    }


def test_sample_keeps_negatives_out_of_corpus(tmp_path):
    rows = [_row(i, "mono_retrieval", [f"u{i}", f"u{i}b", "shared"]) for i in range(5)]
    rows += [_row(10, "mono_retrieval", ["n1", "n2", "n3"])]
    rows += [_row(20, "multi_retrieval", ["m1", "m2", "m3"])]
    groups = xrag.sample(rows, mono=3, multi=1, negative=5, seed=1)
    corpus_urls = {u for r in groups["mono"] + groups["multi"] for u in r["urls"]}
    if all(r["id"] != 10 for r in groups["mono"]):
        assert [r["id"] for r in groups["neg"]] == [10], (
            "only row 10 shares nothing with the corpus"
        )
    assert all(not corpus_urls.intersection(r["urls"]) for r in groups["neg"])

    manifest = xrag.build_corpus(groups, tmp_path)
    assert len(manifest) == len(corpus_urls)  # "shared" written once
    queries = xrag.build_queries(groups, ["de", "en", "multi", "neg"])
    by_variant = {
        v: [q for q in queries if q["variant"] == v] for v in ("de", "en", "multi", "neg")
    }
    assert len(by_variant["de"]) == len(by_variant["en"]) == 3
    assert by_variant["en"][0]["query"].startswith("Question")
    assert all(q["support"] == {} and not q["expect_answer"] for q in by_variant["neg"])
    assert set(by_variant["de"][0]["support"]) <= set(manifest)


def test_aggregate_and_gap():
    rows = [
        {
            "variant": "de",
            "id": 1,
            "judge_score": 0.5,
            "judge_verdict": "partial",
            "processing_ms": 2000,
        },
        {
            "variant": "en",
            "id": 1,
            "judge_score": 1.0,
            "judge_verdict": "correct",
            "processing_ms": 4000,
        },
        {"variant": "de", "id": 2, "error": "boom"},
    ]
    a = aggregate([r for r in rows if r["variant"] == "de"])
    assert a["errors"] == 1 and a["judge_score"] == 0.5 and a["latency_p50_s"] == 2.0
    gap = paired_gap(rows, "de", "en")
    assert gap["n"] == 1 and gap["delta"] == pytest.approx(-0.5)

    neg = aggregate(
        [
            {"variant": "neg", "expect_answer": False, "judge_refused": True},
            {"variant": "neg", "expect_answer": False, "judge_refused": False},
        ]
    )
    assert neg["refusal_rate"] == 0.5 and neg["hallucination_rate"] == 0.5


def test_distractor_limit(tmp_path):
    rows = [_row(1, "mono_retrieval", ["s1", "d1", "s2", "d2", "d3"], support=(2, 0, 1, 0, 0))]
    groups = {"mono": rows, "multi": [], "neg": []}
    names = lambda urls: {xrag.article_file_name(u) for u in urls}  # noqa: E731
    assert set(xrag.build_corpus(groups, tmp_path / "a", distractors=0)) == names(["s1", "s2"])
    assert set(xrag.build_corpus(groups, tmp_path / "b", distractors=1)) == names(
        ["s1", "s2", "d1"]
    )
