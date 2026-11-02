"""`sem site build`: v0.5 jobs -> site/data.js (metrics from the rows, provenance, refusal rules, the page's keys)."""

import json
import os
import re
import subprocess
from typing import ClassVar

import pytest

from simple_evals_mm import checks, errors, leaderboard
from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm.models import Response
from simple_evals_mm.samplers import registry
from tests.fakes import ANSWERS, Arith

SITE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site")
SLUG = "openrouter/fake/model"
ENDPOINT = {"provider_name": "FakeCloud", "tag": "fakecloud/bf16", "quantization": "bf16", "context_length": 32768,
            "max_completion_tokens": 8192, "canonical_slug": "fake/model-20261101", "price_per_mtok": {"prompt": 1.0, "completion": 2.0}}


class Arith2(Arith):
    name = "arith2"
    questions: ClassVar = ["2+2?", "capital of France?"]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def lb(job_env, tmp_path, monkeypatch):
    """job_env + a git checkout with an origin (one pushed and one local-only commit), start checks that record the
    RECORD check and Python, an OpenRouter-like fake model with a pinned endpoint, and a spec writer."""
    s = job_env
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "pushed")
    _git(repo, "push", "-q", "origin", "HEAD")
    pushed = _git(repo, "rev-parse", "HEAD")
    (repo / "b.txt").write_text("b")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "local only")
    s.update(repo=str(repo), pushed=pushed, local=_git(repo, "rev-parse", "HEAD"), commit=pushed)
    monkeypatch.setattr(checks, "start_checks", lambda allow_dirty=False, **kw: {
        "version": "0.5.0", "editable": False, "commit": s["commit"], "dirty": False,
        "python": {"version": "3.13.1 (main)", "platform": "linux", "implementation": "CPython"},
        "lock_check": {"ok": True, "lock_sha256": "l", "mismatches": []},
        "record_check": {"checked": 10, "ok": True, "mismatches": [], "not_installed": []}})
    monkeypatch.setitem(dataset_mod.DATASETS, "arith2", Arith2)
    monkeypatch.setitem(registry.PRESETS, SLUG, {})
    monkeypatch.setitem(registry.PRESETS, "openrouter/fake/alias", {"alias": True})
    smp = s["sampler"]
    smp.reply = lambda q: Response(text=ANSWERS[q], input_tokens=5, output_tokens=10, cost_usd=0.001,
                                   finish_reason="length" if q == "cut" else "stop", model="fake/model-20261101",
                                   provider="FakeCloud")
    smp.config = lambda: {"api_format": "openai_chat_completions", "provider": "openrouter", "model": "fake/model",
                          "preset": SLUG, "base_url": "https://openrouter.ai/api/v1", "endpoint": dict(ENDPOINT),
                          "settings": {"temperature": {"value": 0.6, "source": "preset:fake"},
                                       "top_k": {"value": None, "source": "provider default (not reported)"},
                                       "max_output_tokens": {"value": 8192, "source": "openrouter:endpoint max_completion_tokens"}}}

    def run(name, *extra, model=SLUG, datasets="arith"):
        return s["sem"]("run", "-m", model, "-d", datasets, "--job-name", name, "-o", s["jobs"], "-n", "1", *extra)

    def spec(settings, tasks=("arith",), path=None, **row):
        doc = {"jobs_dir": s["jobs"], "site": {"name": "t", "code_url": "https://example.com/r"},
               "categories": ["Text"],
               "tasks": {t: {"label": t.upper(), "category": "Text", "lang": "en", "grading": "exact"} for t in tasks},
               "benchmarks": [{"key": "Overall", "label": "Overall", "overall": True}],
               "models": [{"key": "m", "name": "Model", "dev": "Dev", "type": "open", "params_b": 7, **row,
                           "settings": settings}]}
        p = path or str(tmp_path / "leaderboard.yaml")
        with open(p, "w") as f:
            json.dump(doc, f)  # JSON is YAML
        return p

    def build(p):
        return leaderboard.build(p, repo=s["repo"], today="2026-11-02")

    s.update(run_lb=run, spec=spec, build=build)
    return s


def _refused(report, dataset="arith"):
    return [r for x in report["refused"] if x["dataset"] in (dataset, "*") for r in x["reasons"]]


def _edit_json(path, fn):
    with open(path) as f:
        d = json.load(f)
    fn(d)
    with open(path, "w") as f:
        json.dump(d, f)


def test_clean_job_metrics_and_provenance(lb, monkeypatch):
    s = lb
    monkeypatch.setattr(Arith, "questions", ["2+2?", "3+3?", "capital of France?", "cut", "flaky"])
    s["sampler"].fail = {"flaky": errors.SamplerError("503 overloaded")}
    assert s["run_lb"]("j", "-k", "2", "-r", "0") == 0
    data, report = s["build"](s["spec"]([{"label": "low", "job": "j"}]))
    assert report["refused"] == [] and report["shown"] == 1
    row = data["models"][0]
    assert row["maxLabel"] == "low" and "low" not in row and row["model"] == SLUG
    lvl = row["max"]
    c = lvl["tasks"]["arith"]
    # per attempt: 2+2 right, 3+3 wrong, Paris right, cut = 0 (budget_exhausted), flaky excluded -> 2 / 4
    assert c["score"] == pytest.approx(50.0) and c["std"] == pytest.approx(0.0) and c["n_repeats"] == 2
    assert c["causes"] == {"budget_exhausted": 2, "provider_failed": 2} and c["zeroed"] == 2 and c["excluded"] == 2
    assert c["causes_per_attempt"] == [{"budget_exhausted": 1, "provider_failed": 1}] * 2
    assert c["categories"] == {"math": pytest.approx(50.0), "other": pytest.approx(50.0)}
    assert c["cost"] == pytest.approx(0.001 * 8 / 10)  # 8 answered trials paid, 10 trials
    assert c["tok"] == pytest.approx(0.010)  # 10 output tokens per answered trial, in K
    assert lvl["benchmarks"]["Overall"]["score"] == pytest.approx(50.0)
    st = s["store"]("j")
    lock = st.read_lock()
    p = c["prov"]
    assert p["job_id"] == lock["job_id"] and p["revision"] == "test-rev" and p["data_lock"] == "ok"
    assert p["tasks_sha256"] == lock["datasets"]["arith"]["tasks_sha256"] and p["n_tasks"] == 5 and p["n_attempts"] == 2
    assert p["verification_id"] == st.current_verification_id("arith") and p["verifier_lock"]
    assert p["identity"]["provider"] == ["FakeCloud"] and p["identity"]["changes"] == 0
    j = lvl["jobs"][lock["job_id"]]
    assert j["job"] == "j" and j["commit"] == s["pushed"] and j["version"] == "0.5.0" and j["python"] == "3.13.1"
    assert j["model"] == SLUG and j["model_identity"] == "pinned" and j["sessions"] == 1
    assert j["endpoint"] == {k: ENDPOINT[k] for k in ("provider_name", "tag", "quantization", "context_length", "max_completion_tokens")}
    assert j["base_url_host"] == "openrouter.ai" and j["settings"]["temperature"] == {"value": 0.6, "source": "preset:fake"}
    assert j["served_identity"] == {"canonical_slug": "fake/model-20261101"} and "--job-name j" in j["command"]
    assert lvl["provider"] == "FakeCloud" and lvl["quantization"] == "bf16" and lvl["alias"] is False
    assert data["tasks"][0]["n"] == 5 and data["site"]["version"] == "0.5.0" and data["site"]["n_runs"] == 2
    assert data["causes"]["zeroed"] == ["budget_exhausted", "refused", "context_overflow"]


def test_row_combines_two_jobs_and_marks_alias(lb):
    s = lb
    assert s["run_lb"]("a", datasets="arith") == 0
    assert s["run_lb"]("b", datasets="arith2", model="openrouter/fake/alias") == 0
    data, report = s["build"](s["spec"]([{"label": "low", "job": "a", "datasets": {"arith2": "b"}}], tasks=("arith", "arith2")))
    assert report["refused"] == []
    lvl = data["models"][0]["max"]
    ja, jb = s["store"]("a").read_lock()["job_id"], s["store"]("b").read_lock()["job_id"]
    assert set(lvl["jobs"]) == {ja, jb}
    assert lvl["tasks"]["arith"]["prov"]["job_id"] == ja and lvl["tasks"]["arith2"]["prov"]["job_id"] == jb
    assert lvl["tasks"]["arith2"]["score"] == pytest.approx(100.0)
    assert lvl["alias"] is True and lvl["jobs"][jb]["model_identity"] == "alias"
    assert lvl["benchmarks"]["Overall"]["score"] == pytest.approx((100 * 2 / 3 + 100) / 2)
    assert data["models"][0]["model"] is None  # two models: the spec gives none, the jobs disagree


def _clean(lb, *extra, name="j"):
    assert lb["run_lb"](name, *extra) == 0
    return lb["store"](name)


REFUSALS = {
    "dirty": (lambda st, lb: _edit_json(os.path.join(st.dir, "lock.json"), lambda d: d["code"].update(dirty=True)),
              "dirty job"),
    "lock check": (lambda st, lb: _edit_json(os.path.join(st.dir, "lock.json"),
                                             lambda d: d["code"]["lock_check"].update(ok=False)), "uv.lock check failed"),
    "record check": (lambda st, lb: _edit_json(os.path.join(st.dir, "lock.json"),
                                               lambda d: d["code"]["record_check"].update(ok=False)), "RECORD check failed"),
    "data mismatch": (lambda st, lb: _edit_json(os.path.join(st.dir, "lock.json"),
                                                lambda d: d["datasets"]["arith"].update(data_lock="mismatch")),
                      "data differs from datasets.lock"),
    "data none": (lambda st, lb: _edit_json(os.path.join(st.dir, "lock.json"),
                                            lambda d: d["datasets"]["arith"].update(data_lock="none")),
                  "no datasets.lock entry"),
    "verification dirty": (lambda st, lb: _edit_json(os.path.join(st.verifier_dir("arith"), st.current_verification_id("arith"), "meta.json"),
                                                     lambda d: d.update(dirty=True)), "--allow-dirty"),
    "task change": (lambda st, lb: _edit_json(os.path.join(st.verifier_dir("arith"), st.current_verification_id("arith"), "meta.json"),
                                              lambda d: d.update(task_changes=["2+2?"])), "--allow-task-change"),
    "trials edited": (lambda st, lb: _replace(st.trials_path("arith", 1), '"4"', '"5"'), "job check: arith/trials_a1.jsonl: sha256 differs"),
}


def _replace(path, a, b):
    with open(path) as f:
        text = f.read()
    with open(path, "w") as f:
        f.write(text.replace(a, b, 1))


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_refusals_from_job_files(lb, case):
    st = _clean(lb)
    edit, reason = REFUSALS[case]
    edit(st, lb)
    data, report = lb["build"](lb["spec"]([{"label": "low", "job": "j"}]))
    assert data["models"] == [] and any(reason in r for r in _refused(report)), report


def test_refusal_commit_not_pushed_or_unknown(lb):
    lb["commit"] = lb["local"]
    _clean(lb, name="local")
    lb["commit"] = "0" * 40
    _clean(lb, name="unknown")
    _, report = lb["build"](lb["spec"]([{"label": "a", "job": "local"}, {"label": "b", "job": "unknown"}]))
    reasons = {x["setting"]: " ".join(x["reasons"]) for x in report["refused"]}
    assert "is not on origin (not pushed)" in reasons["a"] and "not verified" in reasons["b"]
    _, report = leaderboard.build(lb["spec"]([{"label": "a", "job": "local"}]), repo="/nonexistent")
    assert "not verified" in " ".join(_refused(report))


def test_refusal_incomplete_attempts(lb):
    lb["sampler"].fail = {"capital": KeyboardInterrupt()}
    with pytest.raises(KeyboardInterrupt):
        lb["run_lb"]("j")
    _, report = lb["build"](lb["spec"]([{"label": "low", "job": "j"}]))
    reasons = " ".join(_refused(report))
    assert "attempts incomplete (0 of 1 complete)" in reasons and "no current verification" in reasons


def test_refusal_no_and_stale_verification(lb, monkeypatch):
    monkeypatch.setattr(Arith, "questions", ["2+2?", "flaky"])
    lb["sampler"].fail = {"flaky": errors.SamplerError("503 overloaded")}
    st = _clean(lb, "--generate-only", "-r", "0")
    _, report = lb["build"](lb["spec"]([{"label": "low", "job": "j"}]))
    assert any("no current verification" in r for r in _refused(report))
    assert lb["sem"]("job", "regrade", st.dir) == 0
    _, report = lb["build"](lb["spec"]([{"label": "low", "job": "j"}]))
    assert report["refused"] == []
    lb["sampler"].fail = {}
    assert lb["sem"]("job", "resume", "-p", st.dir, "-f", "provider_failed") == 0  # generate-only: not re-verified
    _, report = lb["build"](lb["spec"]([{"label": "low", "job": "j"}]))
    assert any("is stale: a1 trials changed" in r for r in _refused(report))


def test_refusal_served_without_manifest_selection_and_spec(lb):
    assert lb["run"](name="served") == 0  # sglang/fake, no --server manifest
    _clean(lb, "-l", "2", name="subset")
    _clean(lb, name="ok")
    p = lb["spec"]([{"label": "a", "job": "served"}, {"label": "b", "job": "subset"}], model=SLUG)
    _, report = lb["build"](p)
    reasons = {x["setting"]: " ".join(x["reasons"]) for x in report["refused"]}
    assert "served model (sglang) without a server manifest" in reasons["a"] and "is not the row's model" in reasons["a"]
    assert "task selection {'n_tasks': 2}" in reasons["b"]
    _, report = lb["build"](lb["spec"]([{"label": "a", "job": "ok", "effort": "high"},
                                        {"label": "b", "datasets": {"arith": "missing"}}]))
    reasons = {x["setting"]: " ".join(x["reasons"]) for x in report["refused"]}
    assert "job effort None is not the setting's effort high" in reasons["a"] and "not found" in reasons["b"]


def test_allow_refs_accepts_a_recorded_ref(lb):
    st = _clean(lb)
    _edit_json(os.path.join(st.dir, "lock.json"), lambda d: d["datasets"]["arith"].update(data_lock="none"))
    _edit_json(os.path.join(st.dir, "config.json"), lambda d: d.update(datasets=["arith@v2"]))
    view = leaderboard.JobView(st.dir, leaderboard.GitCheck(lb["repo"]))
    assert any("no datasets.lock entry for ref @v2" in r for r in leaderboard.dataset_reasons(view, "arith"))
    assert leaderboard.dataset_reasons(view, "arith", allow_ref="v2") == []


def test_cli_build_writes_loadable_data_js(lb, tmp_path, capsys):
    _clean(lb)
    out = tmp_path / "site" / "data.js"
    p = lb["spec"]([{"label": "low", "job": "j"}, {"label": "high", "job": "missing"}])
    assert lb["sem"]("site", "build", p, "-o", str(out), "--repo", lb["repo"]) == 0
    assert "1 refused" in capsys.readouterr().out
    assert lb["sem"]("site", "build", p, "-o", str(out), "--repo", lb["repo"], "--strict") == 1
    text = out.read_text()
    assert text.startswith("window.LB = ") and text.rstrip().endswith(";")
    data = json.loads(text[len("window.LB = "):].rstrip().rstrip(";"))
    assert data["models"][0]["max"]["tasks"]["arith"]["score"] == pytest.approx(100 * 2 / 3)


# ---------------------------------------------------------------- the keys site/index.html reads
def _reads(text: str, var: str) -> set[str]:
    return set(re.findall(rf"(?<![\w.]){re.escape(var)}\.([A-Za-z_]\w*)", text))


JS_BUILTINS = {"length", "map", "filter", "slice", "toFixed", "join", "some", "every", "reduce", "find", "includes",
               "startsWith", "localeCompare", "toLocaleString", "replace", "flatMap", "sort", "forEach", "split",
               "style", "textContent"}  # DOM nodes that share a name (t = a <text> element in the chart)


def test_data_js_has_the_keys_the_page_reads(lb):
    """Every `D.x`, `SITE.x`, model `m.x`, level `lvl.x`, task / cell `t.x`, picked value `d.x`, job `j.x`, cell
    `c.x` and provenance `p.x` (inside settingsHTML) that site/index.html reads exists in the built data."""
    assert lb["run_lb"]("a", "-k", "2") == 0
    assert lb["run_lb"]("b", "-k", "2") == 0
    data, report = lb["build"](lb["spec"]([{"label": "low", "job": "a"}, {"label": "high", "job": "b"}]))
    assert not report["refused"]
    with open(os.path.join(SITE, "index.html"), encoding="utf-8") as f:
        html = f.read()
    script = html[html.index("const D = window.LB;"):]
    panel = script[script.index("function settingsHTML"):script.index("function renderTable")]
    row, lvl = data["models"][0], data["models"][0]["max"]
    cell = lvl["tasks"]["arith"]
    job = next(iter(lvl["jobs"].values()))
    optional = {"jammeval_paper", "jammeval_url", "jammeval_note"}  # spec texts the page falls back from

    def check(var, have, text=script, extra=()):
        missing = _reads(text, var) - set(have) - JS_BUILTINS - set(extra)
        assert not missing, f"index.html reads {var}.{sorted(missing)}, not in data.js"

    check("D", data, extra=optional)
    check("SITE", data["site"])
    check("m", row)
    check("lvl", lvl)
    check("t", set(data["tasks"][0]) | set(cell) | {"jammeval", "paper", "dataset", "description"})
    check("d", cell)
    check("j", job, text=panel)
    check("c", cell, text=panel)
    check("p", cell["prov"], text=panel)
    check("ep", job["endpoint"], text=panel)
    assert {"zeroed", "excluded"} <= set(data["causes"])


def test_served_job_needs_a_matching_manifest_digest():
    from simple_evals_mm.serving import manifest as server_mf

    mf = {"format": server_mf.FORMAT, "engine": {"name": "sglang", "version": "0.5.23", "patches": [{"name": "p"}]}}
    lock = {"code": {"commit": "c", "lock_check": {"ok": True}, "record_check": {"ok": True}},
            "sampler": {"provider": "sglang"}, "server": {"digest": server_mf.digest(mf), "manifest": mf}}
    ok = leaderboard.GitCheck(None)
    ok.cache["c"] = None
    assert leaderboard.job_reasons({}, lock, ok) == []
    lock["server"]["manifest"] = {**mf, "engine": {**mf["engine"], "version": "0.5.24"}}
    assert leaderboard.job_reasons({}, lock, ok) == ["server manifest digest does not match the recorded manifest"]


def test_committed_spec_loads(tmp_path):
    spec = leaderboard.load_spec(os.path.join(SITE, "leaderboard.yaml"))
    assert spec["models"] and all(len(m["settings"]) in (1, 2) for m in spec["models"])
    data, report = leaderboard.build(os.path.join(SITE, "leaderboard.yaml"), jobs_dir=str(tmp_path), repo="/nonexistent")
    assert data["models"] == [] and report["refused"] and all("not found" in r["reasons"][0] for r in report["refused"])
    assert [t["key"] for t in data["tasks"]] == list(spec["tasks"])
