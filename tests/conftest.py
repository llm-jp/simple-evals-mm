import pytest

from simple_evals_mm import runs


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    """Point the run-centric layout (runs/, runs/index.jsonl) at a temporary directory."""
    d = tmp_path / "runs"
    monkeypatch.setattr(runs, "RUNS_DIR", str(d))
    monkeypatch.setattr(runs, "INDEX", str(d / "index.jsonl"))
    return d
from tests.fakes import image_dir, job_env  # noqa: F401 - fixtures of the v0.5 tests
