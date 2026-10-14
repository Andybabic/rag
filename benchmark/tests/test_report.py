from report import _sign_test, config_diff, paired_runs


def _row(key: str, score: float, verdict: str) -> dict:
    return {"key": key, "query": f"q {key}", "judge_score": score, "judge_verdict": verdict,
            "recall_all": 0.5}


def test_sign_test_balanced_split_is_noise():
    assert _sign_test(10, 10) == 1.0
    assert _sign_test(0, 0) is None


def test_sign_test_lopsided_split_is_significant():
    assert _sign_test(15, 2) < 0.01


def test_config_diff_separates_sample_and_pipeline():
    prev = {"seed": 42, "pipeline": {"reranker": {"enabled": True, "model": "bge"}}}
    cur = {"seed": 7, "pipeline": {"reranker": {"enabled": False, "model": "bge"}}}
    diff = config_diff(cur, prev)
    assert diff["sample"] == {"seed": (42, 7)}
    assert diff["pipeline"] == {"reranker.enabled": (True, False)}


def test_config_diff_unknown_without_pipeline():
    assert config_diff({"pipeline": {"a": 1}}, {})["pipeline"] is None


def test_paired_runs_counts_and_flips():
    prev = [_row("a", 1.0, "correct"), _row("b", 0.0, "incorrect"), _row("c", 0.5, "partial")]
    cur = [_row("a", 0.0, "incorrect"), _row("b", 1.0, "correct"), _row("c", 0.5, "partial"),
           _row("d", 1.0, "correct")]  # d only in the new run: not paired
    paired = paired_runs(cur, prev)
    assert paired["n"] == 3
    assert (paired["better"], paired["worse"], paired["unchanged"]) == (1, 1, 1)
    assert [f["key"] for f in paired["now_correct"]] == ["b"]
    assert [f["key"] for f in paired["now_wrong"]] == ["a"]


def test_paired_runs_skips_errors():
    prev = [_row("a", 1.0, "correct")]
    cur = [{**_row("a", 0.0, "incorrect"), "error": "timeout"}]
    assert paired_runs(cur, prev) is None


def test_config_diff_reports_model_change():
    prev = {"kermit_model": "ministral-3:14b", "pipeline": {"x": 1}}
    cur = {"kermit_model": "gemma-4-26b", "pipeline": {"x": 1}}
    assert config_diff(cur, prev)["pipeline"] == {"kermit_model": ("ministral-3:14b", "gemma-4-26b")}
