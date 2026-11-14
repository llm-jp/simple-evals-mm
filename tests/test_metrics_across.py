from math import comb

from simple_evals_mm import metrics
from simple_evals_mm.models import VerifierResult


def _r(task, reward, extracted=None, method="exact"):
    return VerifierResult(task, reward, extracted=extracted, method=method)


def test_pass_at_k_matches_the_combinatorial_formula():
    for n, c, k in [(3, 1, 2), (10, 3, 5), (5, 0, 1), (5, 5, 3)]:
        assert abs(metrics.pass_at_k(n, c, k) - (1 - comb(n - c, k) / comb(n, k))) < 1e-12


def test_pass_at_1_is_the_mean_and_pass_at_k_rewards_variety():
    # A: the same two of four questions right in every attempt; B: every question right in one of two attempts
    a = {k: [_r("q1", 1), _r("q2", 1), _r("q3", 0), _r("q4", 0)] for k in (1, 2)}
    b = {1: [_r("q1", 1), _r("q2", 1), _r("q3", 0), _r("q4", 0)], 2: [_r("q1", 0), _r("q2", 0), _r("q3", 1), _r("q4", 1)]}
    pa, pb = metrics.across_attempts(a)["pass_at_k"], metrics.across_attempts(b)["pass_at_k"]
    assert pa[1] == pb[1] == 0.5
    assert pa[2] == 0.5 and pb[2] == 1.0


def test_ci_contains_the_score_and_is_deterministic():
    res = {k: [_r(f"q{i}", float(i % 4 == 0 or (i + k) % 5 == 0)) for i in range(60)] for k in (1, 2, 3)}
    out = metrics.across_attempts(res)
    score = sum(r.reward for rs in res.values() for r in rs) / 180
    assert out["ci95"][0] < score < out["ci95"][1] and out["n_questions"] == 60
    assert metrics.across_attempts(res) == out


def test_excluded_trials_are_left_out_and_non_binary_has_no_pass_at_k():
    res = {1: [_r("q1", 0.5), _r("q2", None)], 2: [_r("q1", 0.7), _r("q2", 1.0)]}
    out = metrics.across_attempts(res)
    assert "pass_at_k" not in out  # 0.5 / 0.7 are not binary
    assert out["n_questions"] == 2


def test_majority_uses_the_most_common_extracted_answer_for_exact_answers_only():
    res = {1: [_r("q1", 1, "A"), _r("q2", 0, "C")], 2: [_r("q1", 0, "B"), _r("q2", 1, "D")],
           3: [_r("q1", 1, "A"), _r("q2", 0, "C")]}
    assert metrics.across_attempts(res)["majority"] == 0.5  # q1: A (right), q2: C (wrong)
    judged = {k: [_r("q1", 1, "x", method="judge")] for k in (1, 2)}
    assert "majority" not in metrics.across_attempts(judged)


def test_plain_mean_only_for_the_default_metrics():
    assert metrics.plain_mean(metrics.DEFAULT)
    assert not metrics.plain_mean((metrics.mean(), metrics.corpus()))
