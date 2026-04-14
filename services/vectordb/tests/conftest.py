from __future__ import annotations

import pytest
from core.qdrant import set_client
from qdrant_client import QdrantClient


@pytest.fixture(autouse=True)
def _qdrant_in_memory():
    """Use an in-memory Qdrant client for all tests."""
    client = QdrantClient(":memory:")
    set_client(client)
    yield
    set_client(None)
