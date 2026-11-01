"""v0.5 start checks."""

import subprocess

import pytest

from simple_evals_mm import checks


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "m.py").write_text("x = 1\n")
    (r / ".gitignore").write_text("*.secret.py\n")
    _git(r, "init", "-q")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    lock = tmp_path / "uv.lock"
    lock.write_text('version = 1\n[[package]]\nname = "simple-evals-mm"\nversion = "9"\nsource = { editable = "." }\n')
    return r, str(lock)


def test_clean_tree_check(repo):
    r, lock = repo
    kw = {"repo": str(r), "lock_path": lock, "package_dir": str(r / "pkg"), "do_import": False}
    out = checks.start_checks(**kw)
    assert out["dirty"] is False and len(out["commit"]) == 40 and out["lock_check"]["ok"]
    (r / "pkg" / "new.py").write_text("y = 2\n")
    with pytest.raises(checks.CheckError, match="uncommitted"):
        checks.start_checks(**kw)
    out = checks.start_checks(allow_dirty=True, **kw)
    assert out["dirty"] and "new.py" in out["diff"] and "y = 2" in out["diff"]
    (r / "pkg" / "new.py").unlink()
    (r / "pkg" / "x.secret.py").write_text("")  # ignored, so invisible to git status
    with pytest.raises(checks.CheckError, match="ignored .py"):
        checks.start_checks(**kw)


def test_lock_check(tmp_path):
    import yaml

    lock = tmp_path / "uv.lock"
    lock.write_text(f'version = 1\n[[package]]\nname = "PyYAML"\nversion = "{yaml.__version__}"\n'
                    '[[package]]\nname = "not-installed-pkg"\nversion = "1"\n')
    assert checks.lock_check(str(lock))["ok"]
    lock.write_text('version = 1\n[[package]]\nname = "pyyaml"\nversion = "0.0.1"\n')
    res = checks.lock_check(str(lock))
    assert not res["ok"] and res["mismatches"][0]["locked"] == "0.0.1"
    assert not checks.lock_check(str(tmp_path / "missing.lock"))["ok"]
