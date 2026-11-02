from tests import golden
from tests.fakes import image_dir, job_env  # noqa: F401 - fixtures of the tests


def pytest_addoption(parser):
    parser.addoption("--update-golden", action="store_true", help="rewrite the tests/golden/ snapshots the run touches")


def pytest_configure(config):
    golden.UPDATE = config.getoption("--update-golden")


import pytest

from simple_evals_mm import runs


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    """Point the run-centric layout (runs/, runs/index.jsonl) at a temporary directory."""
    d = tmp_path / "runs"
    monkeypatch.setattr(runs, "RUNS_DIR", str(d))
    monkeypatch.setattr(runs, "INDEX", str(d / "index.jsonl"))
    return d
