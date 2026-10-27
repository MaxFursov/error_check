import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture(autouse=True)
def _no_real_seed(monkeypatch, tmp_path):
    """Тести не повинні підхоплювати справжній state_seed.json з репозиторію."""
    import state
    monkeypatch.setattr(state, "SEED_PATH", tmp_path / "no_seed.json")


@pytest.fixture(autouse=True)
def _descriptions_defaults(monkeypatch):
    """Тести описів за замовчуванням працюють у «простому» режимі й з усіма видами товарів."""
    monkeypatch.setenv("DESC_ONLY_KINDS", "")
    monkeypatch.setenv("DESC_RESEARCH", "false")
