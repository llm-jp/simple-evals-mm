"""v0.5 start checks."""

import subprocess

import pytest

from simple_evals_mm import checks


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, msg="c"):
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _version(repo):
    """The version hatch-vcs gives the repo's HEAD between tags."""
    return f"0.1.dev1+g{_git(repo, 'rev-parse', 'HEAD')[:9]}"


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
    kw = {"repo": str(r), "lock_path": lock, "package_dir": str(r / "pkg"), "do_import": False, "verify_record": False,
          "version": _version(r)}
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


def test_python_and_strict_import(repo, monkeypatch):
    r, lock = repo
    kw = {"repo": str(r), "lock_path": lock, "package_dir": str(r / "pkg"), "verify_record": False, "version": _version(r)}
    monkeypatch.setattr(checks, "_IMPORTED", {"imported": 1, "skipped": [], "failed": {"simple_evals_mm.x": "ImportError: y"}})
    with pytest.raises(checks.CheckError, match="failed to import"):
        checks.start_checks(**kw)
    out = checks.start_checks(allow_dirty=True, **kw)
    assert out["dirty"] and "simple_evals_mm.x" in out["problems"][0] and out["python"]["version"]


def test_record_verify(tmp_path):
    """Installed files are checked against the wheel RECORD (sha256, urlsafe base64)."""
    import base64
    import hashlib
    import importlib.metadata as md

    site = tmp_path / "site"
    (site / "mypkg").mkdir(parents=True)
    (site / "mypkg" / "__init__.py").write_text("x = 1\n")
    info = site / "mypkg-1.0.dist-info"
    info.mkdir()
    h = base64.urlsafe_b64encode(hashlib.sha256(b"x = 1\n").digest()).rstrip(b"=").decode()
    (info / "METADATA").write_text("Name: mypkg\nVersion: 1.0\n")
    (info / "RECORD").write_text(f"mypkg/__init__.py,sha256={h},6\nmypkg-1.0.dist-info/RECORD,,\n")

    def dist(name):
        if name != "mypkg":
            raise md.PackageNotFoundError(name)
        return md.PathDistribution(info)

    assert checks.record_verify(("mypkg", "absent"), distribution=dist) == {"checked": 1, "mismatches": [], "missing": ["absent"]}
    (site / "mypkg" / "__init__.py").write_text("x = 2\n")  # edited in site-packages
    assert checks.record_verify(("mypkg",), distribution=dist)["mismatches"] == ["mypkg: mypkg/__init__.py differs from its RECORD"]


def test_git_dependency_commit(tmp_path, monkeypatch):
    """A git-sourced package must be installed at the commit uv.lock names (direct_url.json commit_id)."""
    import yaml

    lock = tmp_path / "uv.lock"
    lock.write_text(f'version = 1\n[[package]]\nname = "pyyaml"\nversion = "{yaml.__version__}"\n'
                    'source = { git = "https://example/x.git?rev=abc#abc123" }\n')
    monkeypatch.setattr(checks, "dist_identity", lambda name: {"version": yaml.__version__, "commit_id": "abc123"})
    assert checks.lock_check(str(lock))["ok"]
    monkeypatch.setattr(checks, "dist_identity", lambda name: {"version": yaml.__version__, "commit_id": "fff"})
    res = checks.lock_check(str(lock))
    assert not res["ok"] and res["mismatches"] == [{"package": "pyyaml", "locked_commit": "abc123", "installed_commit": "fff"}]


def test_dotenv_exports_secrets_only(tmp_path, monkeypatch, capsys):
    from simple_evals_mm import cli

    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=k1\nHF_TOKEN=t\nGEMINI_LOCATION=global\nOPENROUTER_BASE_URL=http://evil\n"
                   "OPENROUTER_TEMP=0.3\nGOOGLE_GENAI_USE_VERTEXAI=1\n")
    for k in ("OPENROUTER_API_KEY", "HF_TOKEN", "GEMINI_LOCATION", "OPENROUTER_BASE_URL", "OPENROUTER_TEMP",
              "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.setenv(k, "")  # restored (unset) at teardown
        monkeypatch.delenv(k)
    monkeypatch.setattr(checks, "ENV_STATE", {})
    out = cli.load_env(str(env))
    assert out == {"exported": ["GEMINI_LOCATION", "HF_TOKEN", "OPENROUTER_API_KEY"],
                   "ignored": ["GOOGLE_GENAI_USE_VERTEXAI", "OPENROUTER_BASE_URL", "OPENROUTER_TEMP"]}
    import os

    assert os.environ["OPENROUTER_API_KEY"] == "k1" and "OPENROUTER_BASE_URL" not in os.environ
    assert checks.ENV_STATE["dotenv_exported"] == out["exported"]
    assert "OPENROUTER_TEMP" in capsys.readouterr().err


def test_version_check_against_head_and_tags(repo):
    """The installed version (from the git tags) must name HEAD: its +g<hash> (a stale editable install after
    `uv run --no-sync` names an older commit), or on an exactly tagged commit the tag itself."""
    r, lock = repo
    head = _git(r, "rev-parse", "HEAD")
    assert checks.version_check(str(r), f"0.1.dev1+g{head[:9]}") is None
    assert checks.version_check(str(r), f"0.1.dev1+g{head[:9]}.d20261103") is None  # built from a dirty tree
    old = head
    new = _commit(r)
    problem = checks.version_check(str(r), f"0.1.dev1+g{old[:9]}")
    assert problem and old[:9] in problem and new[:12] in problem and "--reinstall-package simple-evals-mm" in problem
    assert "is not tagged" in checks.version_check(str(r), "0.5.0")
    _git(r, "tag", "v0.5.0")
    assert checks.version_check(str(r), "0.5.0") is None  # exactly on the tag: no local part
    assert "differs from the tag v0.5.0" in checks.version_check(str(r), "0.4.0")
    assert checks.git_describe(str(r)) == f"v0.5.0-0-g{_git(r, 'rev-parse', '--short=7', 'HEAD')}"
    assert "not installed" in checks.version_check(str(r), None)

    kw = {"repo": str(r), "lock_path": lock, "package_dir": str(r / "pkg"), "do_import": False, "verify_record": False}
    with pytest.raises(checks.CheckError, match="stale editable"):
        checks.start_checks(version=f"0.1.dev1+g{old[:9]}", **kw)
    out = checks.start_checks(allow_dirty=True, version=f"0.1.dev1+g{old[:9]}", **kw)  # recorded, job marked dirty
    assert out["dirty"] and out["version_ok"] is False and any("built at commit" in p for p in out["problems"])
    out = checks.start_checks(version="0.5.0", **kw)
    assert out["describe"].startswith("v0.5.0-0-g") and out["version"] == "0.5.0" and out["version_ok"]


def test_entry_point_and_extras_are_recorded():
    """lock.json sessions record how the process started; the start record lists which optional extras are installed
    (the hf-server modules are never imported by a job, so a venv without the extra passes)."""
    ep = checks.entry_point()
    assert set(ep) == {"argv0", "main_module"} and ep["argv0"]
    ex = checks.extras_info()
    assert set(ex) == {"hf-server"} and isinstance(ex["hf-server"]["installed"], bool)
    assert "simple_evals_mm.serving.hf_server" in checks.SKIP_IMPORT and "simple_evals_mm.serving.hf_backends" in checks.SKIP_IMPORT
