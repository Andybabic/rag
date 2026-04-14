"""Shared fixtures for end-to-end integration tests."""

from __future__ import annotations

import os

import httpx
import pytest


def _url(service: str) -> str:
    """Resolve service base URL from environment."""
    mapping = {
        "cleaning": os.getenv(
            "CLEANING_SERVICE_URL", "http://cleaning:8001"
        ),
        "data_structure": os.getenv(
            "DATA_STRUCTURE_SERVICE_URL", "http://data_structure:8002"
        ),
        "embedding": os.getenv(
            "EMBEDDING_SERVICE_URL", "http://embedding:8003"
        ),
        "vectordb": os.getenv(
            "VECTORDB_SERVICE_URL", "http://vectordb:8004"
        ),
        "evaluation": os.getenv(
            "EVALUATION_SERVICE_URL", "http://evaluation:8005"
        ),
        "frontend": os.getenv(
            "FRONTEND_URL", "http://frontend:3000"
        ),
    }
    return mapping[service]


@pytest.fixture(scope="session")
def service_urls() -> dict[str, str]:
    return {
        name: _url(name)
        for name in [
            "cleaning",
            "data_structure",
            "embedding",
            "vectordb",
            "evaluation",
            "frontend",
        ]
    }


@pytest.fixture(scope="session")
def http_client():
    return httpx.Client(timeout=httpx.Timeout(120.0, connect=30.0))
