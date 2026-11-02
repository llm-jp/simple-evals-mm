from tests import golden
from tests.fakes import (  # noqa: F401 - fixtures of the tests
    data_lock,
    image_dir,
    job_env,
)


def pytest_addoption(parser):
    parser.addoption("--update-golden", action="store_true", help="rewrite the tests/golden/ snapshots the run touches")


def pytest_configure(config):
    golden.UPDATE = config.getoption("--update-golden")

