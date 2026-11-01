"""v0.5 error policy."""


import pytest

from simple_evals_mm import errors


@pytest.mark.parametrize("msg,cause", [
    ("Error code: 400 - data_inspection_failed", errors.REFUSED),
    ("This model's maximum context length is 8192 tokens", errors.CONTEXT_OVERFLOW),
    ("Your input was flagged by moderation", errors.REFUSED),
    ("user_blocked: content policy", errors.PROVIDER_FAILED),
    ("502 Bad Gateway", errors.PROVIDER_FAILED),
])
def test_classify_error(msg, cause):
    assert errors.classify_error(msg) == cause
    assert errors.SamplerError(msg).cause == cause


@pytest.mark.parametrize("finish,cause", [("length", errors.BUDGET_EXHAUSTED), ("content_filter", errors.REFUSED),
                                          ("stop", None), ("", None)])
def test_classify_finish(finish, cause):
    assert errors.classify_finish(finish) == cause


def test_policy_table():
    assert [c for c, p in errors.POLICY.items() if p.retry] == [errors.PROVIDER_FAILED, errors.SANDBOX_FAILED, errors.JUDGE_FAILED]
    assert all(errors.POLICY[c].reward == 0.0 for c in (errors.BUDGET_EXHAUSTED, errors.REFUSED, errors.CONTEXT_OVERFLOW))
