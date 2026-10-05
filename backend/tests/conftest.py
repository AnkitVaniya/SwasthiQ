import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.store import ClinicStore  # noqa: E402


@pytest.fixture(scope="session")
def clinic():
    return json.load(open(ROOT / "backend/data/clinic.json", encoding="utf-8"))


@pytest.fixture
def store(clinic):
    return ClinicStore(clinic, ":memory:", "2026-10-01")
