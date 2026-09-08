from __future__ import annotations

import pytest

from app.communication_engine.config import default_config
from app.communication_engine.engine import CommunicationEngine
from app.communication_engine.models.classification import ReviewInput


@pytest.fixture(scope="session")
def engine() -> CommunicationEngine:
    return CommunicationEngine.build()


@pytest.fixture(scope="session")
def registry(engine):
    return engine.registry


@pytest.fixture(scope="session")
def config():
    return default_config()


def review(**kw) -> ReviewInput:
    return ReviewInput(**kw)
