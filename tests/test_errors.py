"""v0.5 error policy."""


import pytest

from simple_evals_mm import errors


@pytest.mark.parametrize("msg,cause", [
    ("Error code: 400 - data_inspection_failed", errors.REFUSED),
    ("This model's maximum context length is 8192 tokens", errors.CONTEXT_OVERFLOW),
    ("Your input was flagged by moderation", errors.REFUSED),
    ("user_blocked: content policy", errors.PROVIDER_FAILED),
    ("502 Bad Gateway", errors.PROVIDER_FAILED),
    ("Error code: 404 - {'error': {'message': 'No endpoints found for qwen/qwen3.8-27b.', 'code': 404}}", errors.ENDPOINT_UNAVAILABLE),
    ("No endpoints found matching your data policy (Free model training)", errors.ENDPOINT_UNAVAILABLE),
    ("No allowed providers are available for the selected model.", errors.ENDPOINT_UNAVAILABLE),
    ("upstream error: {'code': 'model_not_found', 'message': 'The model does not exist'}", errors.ENDPOINT_UNAVAILABLE),
    ("Error code: 404 - DeploymentNotFound", errors.ENDPOINT_UNAVAILABLE),
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


def test_endpoint_unavailable_policy():
    p = errors.POLICY[errors.ENDPOINT_UNAVAILABLE]
    assert (p.retry, p.counts_in_score, p.reward) == (False, False, None)
    assert not errors.retryable(errors.ENDPOINT_UNAVAILABLE) and not errors.counts_in_score(errors.ENDPOINT_UNAVAILABLE)
