"""Register fixture modules and reset generated observations for test runs."""

import shutil

import pytest

from tests.fixtures import ROOT

pytest_plugins = (
    "tests.fixtures.docker",
    "tests.fixtures.floci",
    "tests.fixtures.proxy",
    "tests.fixtures.stripe",
    "tests.fixtures.dynamodb",
    "tests.fixtures.lambdas",
    "tests.fixtures.workflows",
)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Reset measurements for execution while preserving reports during inspection."""
    if session.config.option.collectonly or session.config.option.setupplan:
        return
    for name in ("coverage", "htmlcov", "evidence"):
        shutil.rmtree(ROOT / "output" / name, ignore_errors=True)
    (ROOT / "output" / "coverage").mkdir(parents=True)
