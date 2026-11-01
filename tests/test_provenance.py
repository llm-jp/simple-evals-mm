"""provenance.changed_datasets: which datasets a code change between two commits touches."""

import subprocess

from simple_evals_mm import provenance


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


def test_changed_datasets(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    pkg = "src/simple_evals_mm"
    base = {
        f"{pkg}/__init__.py": "",
        f"{pkg}/common.py": "def helper():\n    return 1\n",
        f"{pkg}/verifiers.py": "from simple_evals_mm.common import helper\n",
        f"{pkg}/agent.py": "x = 1\n",
        f"{pkg}/tasks/a.py": "from simple_evals_mm import verifiers\nfrom .util import f\nP = 'a_judge_prompt.txt'\n",
        f"{pkg}/tasks/util.py": "def f():\n    pass\n",
        f"{pkg}/tasks/a_judge_prompt.txt": "judge A\n",
        f"{pkg}/tasks/b.py": "def g():\n    import simple_evals_mm.tasks.c\n",
        f"{pkg}/tasks/c.py": "y = 2\n",
    }
    c1 = _commit(repo, base)
    c2 = _commit(repo, {f"{pkg}/common.py": "def helper():\n    return 2\n"})
    c3 = _commit(repo, {f"{pkg}/tasks/a_judge_prompt.txt": "judge A v2\n", f"{pkg}/tasks/c.py": "y = 3\n"})
    c4 = _commit(repo, {f"{pkg}/agent.py": "x = 2\n"})
    kw = {"repo": str(repo), "package_dir": str(repo / pkg),
          "modules": {"a": "simple_evals_mm.tasks.a", "b": "simple_evals_mm.tasks.b"}}

    out = provenance.changed_datasets(c1, c2, ["a", "b"], **kw)
    assert out["a"]["changed"] and out["a"]["dataset"] == [f"{pkg}/common.py"]  # through verifiers.py
    assert not out["b"]["changed"]
    out = provenance.changed_datasets(c2, c3, ["a", "b"], **kw)
    assert out["a"]["dataset"] == [f"{pkg}/tasks/a_judge_prompt.txt"]  # package data the module names
    assert out["b"]["dataset"] == [f"{pkg}/tasks/c.py"]  # lazy import inside a function
    out = provenance.changed_datasets(c3, c4, ["a", "b"], **kw)
    assert out["a"]["generation"] == out["b"]["generation"] == [f"{pkg}/agent.py"] and not out["a"]["dataset"]
    assert not provenance.changed_datasets(c4, c4, ["a"], **kw)["a"]["changed"]
