from tests import golden
from tests.fakes import image_dir, job_env  # noqa: F401 - fixtures of the tests


def pytest_addoption(parser):
    parser.addoption("--update-golden", action="store_true", help="rewrite the tests/golden/ snapshots the run touches")


def pytest_configure(config):
    golden.UPDATE = config.getoption("--update-golden")

