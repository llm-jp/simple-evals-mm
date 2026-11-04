"""provenance.changed_datasets: which datasets a code change between two commits touches."""

import json
import subprocess

import pytest

from simple_evals_mm import provenance

PKG = "src/simple_evals_mm"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, files: dict) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c")
    return _git(repo, "rev-parse", "HEAD")


def _lock(a=1, b=1, nltk=1):
    return json.dumps({"format": "x", "datasets": {"a": {"tasks_sha256": a}, "b": {"tasks_sha256": b},
                                                   "b[split=test]": {"tasks_sha256": 1}},
                       "_shared": {"nltk": {"commit": nltk}}})


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    base = {
        f"{PKG}/__init__.py": "",
        f"{PKG}/common.py": "def helper():\n    return 1\n",
        f"{PKG}/verifiers.py": "from simple_evals_mm.common import helper\n",
        f"{PKG}/agent.py": "x = 1\n",
        f"{PKG}/store.py": "S = 1\n",
        f"{PKG}/runner.py": "from simple_evals_mm import store, agent\n",
        f"{PKG}/cli.py": "from simple_evals_mm import runner\n",
        f"{PKG}/datalock.py": "P = 'datasets.lock.json'\n",
        f"{PKG}/datasets.lock.json": _lock(),
        f"{PKG}/samplers/__init__.py": "",
        f"{PKG}/samplers/base.py": "B = 1\n",
        f"{PKG}/samplers/openai_chat.py": "from simple_evals_mm.samplers.base import B\n",
        f"{PKG}/samplers/gemini.py": "from simple_evals_mm.samplers.openai_chat import B\n",
        f"{PKG}/tasks/a.py": ("from simple_evals_mm import verifiers, datalock\nfrom .util import f\n"
                              "P = 'a_judge_prompt.txt'\nNLTK = 'nltk'\n"),
        f"{PKG}/tasks/util.py": "def f():\n    pass\n",
        f"{PKG}/tasks/a_judge_prompt.txt": "judge A\n",
        f"{PKG}/tasks/b.py": "from simple_evals_mm import datalock\ndef g():\n    import simple_evals_mm.tasks.c\n",
        f"{PKG}/tasks/c.py": "y = 2\n",
        "README.md": "r\n",
    }
    return repo, _commit(repo, base)


def _kw(repo, **kw):
    return {"repo": str(repo), "package_dir": str(repo / PKG),
            "modules": {"a": "simple_evals_mm.tasks.a", "b": "simple_evals_mm.tasks.b"}, **kw}


def test_changed_datasets(repo):
    repo, c1 = repo
    c2 = _commit(repo, {f"{PKG}/common.py": "def helper():\n    return 2\n"})
    c3 = _commit(repo, {f"{PKG}/tasks/a_judge_prompt.txt": "judge A v2\n", f"{PKG}/tasks/c.py": "y = 3\n"})
    c4 = _commit(repo, {f"{PKG}/agent.py": "x = 2\n"})
    kw = _kw(repo)

    out = provenance.changed_datasets(c1, c2, ["a", "b"], **kw)
    # common.py is imported by verifiers.py only: verification-side for every dataset, not a's own file
    assert out["a"]["verification"] == out["b"]["verification"] == [f"{PKG}/common.py"]
    assert not out["a"]["generation_relevant"] and not out["b"]["generation_relevant"] and out["a"]["changed"]
    out = provenance.changed_datasets(c2, c3, ["a", "b"], **kw)
    assert out["a"]["dataset"] == [f"{PKG}/tasks/a_judge_prompt.txt"]  # package data the module names
    assert out["b"]["dataset"] == [f"{PKG}/tasks/c.py"]  # lazy import inside a function
    out = provenance.changed_datasets(c3, c4, ["a", "b"], **kw)
    assert out["a"]["generation"] == out["b"]["generation"] == [f"{PKG}/agent.py"] and not out["a"]["dataset"]
    assert out["a"]["generation_relevant"] and provenance.generation_files(out["a"]) == [f"{PKG}/agent.py"]
    assert not provenance.changed_datasets(c4, c4, ["a"], **kw)["a"]["changed"]


def test_neutral_and_unknown_files(repo):
    repo, c1 = repo
    # NEUTRAL package files and files outside the package do not count
    c2 = _commit(repo, {f"{PKG}/store.py": "S = 2\n", f"{PKG}/runner.py": "from simple_evals_mm import store\n# x\n",
                        f"{PKG}/cli.py": "# y\n", f"{PKG}/datalock.py": "P = 'datasets.lock.json'  # z\n",
                        "README.md": "r2\n", "tests/test_x.py": "t\n", "docs/x.md": "d\n"})
    out = provenance.changed_datasets(c1, c2, ["a", "b"], **_kw(repo))
    assert not out["a"]["changed"] and not out["b"]["changed"]
    # an unlisted package module nobody imports: unknown means unsafe
    c3 = _commit(repo, {f"{PKG}/newmod.py": "N = 1\n"})
    out = provenance.changed_datasets(c2, c3, ["a", "b"], **_kw(repo))
    assert out["a"]["unknown"] == [f"{PKG}/newmod.py"] and out["a"]["generation_relevant"]
    # another dataset's module is not unknown
    c4 = _commit(repo, {f"{PKG}/tasks/a.py": (repo / PKG / "tasks/a.py").read_text() + "# a2\n"})
    out = provenance.changed_datasets(c3, c4, ["a", "b"], **_kw(repo))
    assert out["a"]["dataset"] == [f"{PKG}/tasks/a.py"] and not out["b"]["changed"]


def test_sampler_modules_of_the_api_format(repo):
    repo, c1 = repo
    c2 = _commit(repo, {f"{PKG}/samplers/gemini.py": "from simple_evals_mm.samplers.openai_chat import B\n# g\n"})
    chat = provenance.sampler_modules("sglang", "openai_chat_completions", "sglang")
    google = provenance.sampler_modules("gemini", "google_generate_content")
    assert chat == ["samplers.openai_chat"] and google == ["samplers.gemini"]
    assert provenance.sampler_modules("x", "unknown_format") is None
    out = provenance.changed_datasets(c1, c2, ["a"], **_kw(repo, sampler_modules=chat))
    assert not out["a"]["changed"]  # gemini.py does not shape a sglang job's requests
    out = provenance.changed_datasets(c1, c2, ["a"], **_kw(repo, sampler_modules=google))
    assert out["a"]["generation"] == [f"{PKG}/samplers/gemini.py"]
    out = provenance.changed_datasets(c1, c2, ["a"], **_kw(repo))  # unknown sampler: every sampler counts
    assert out["a"]["generation"] == [f"{PKG}/samplers/gemini.py"]
    # gemini imports openai_chat: a change there shapes a Gemini job too
    c3 = _commit(repo, {f"{PKG}/samplers/openai_chat.py": "from simple_evals_mm.samplers.base import B\n# c\n"})
    for mods in (chat, google):
        out = provenance.changed_datasets(c2, c3, ["a"], **_kw(repo, sampler_modules=mods))
        assert out["a"]["generation"] == [f"{PKG}/samplers/openai_chat.py"]


def test_datasets_lock_entries_by_content(repo):
    repo, c1 = repo
    c2 = _commit(repo, {f"{PKG}/datasets.lock.json": _lock(b=2)})
    out = provenance.changed_datasets(c1, c2, ["a", "b"], **_kw(repo))
    assert not out["a"]["changed"]  # another dataset's entry
    assert out["b"]["dataset"] == [f"{PKG}/datasets.lock.json:datasets.b"]
    c3 = _commit(repo, {f"{PKG}/datasets.lock.json": _lock(b=2, nltk=2)})
    out = provenance.changed_datasets(c2, c3, ["a", "b"], **_kw(repo))
    # a shared artifact: verification-side, for the datasets whose own code names it
    assert out["a"]["verification"] == [f"{PKG}/datasets.lock.json:_shared.nltk"] and not out["a"]["generation_relevant"]
    assert not out["b"]["changed"]


def test_is_ancestor_and_diff_files(repo):
    repo, c1 = repo
    c2 = _commit(repo, {f"{PKG}/store.py": "S = 3\n"})
    assert provenance.is_ancestor(c1, c2, repo=str(repo)) and not provenance.is_ancestor(c2, c1, repo=str(repo))
    with pytest.raises(ValueError):
        provenance.is_ancestor("0" * 40, c2, repo=str(repo))
    assert provenance.diff_files("diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n") == {"src/x.py"}
