"""Unit tests for the public pipeline API: token scope and request validation.

Everything here is pure logic — no database. The DB-backed parts (token
lookup, job claiming) belong to the integration suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from core.api_tokens import TOKEN_PREFIX, ApiToken, generate_token, hash_token
from pydantic import ValidationError
from router.public import ChatRequest


def _token(**overrides) -> ApiToken:
    base = dict(
        id="00000000-0000-0000-0000-000000000001",
        name="test",
        token_prefix="kmt_abcdefgh",
        use_cases=[],
        created_by="admin",
        created_at="2026-01-01T00:00:00+00:00",
        last_used_at=None,
        expires_at=None,
        revoked_at=None,
    )
    base.update(overrides)
    return ApiToken(**base)


# ── token secrets ─────────────────────────────────────────────


def test_generated_token_is_prefixed_and_unique():
    first, second = generate_token(), generate_token()
    assert first.startswith(TOKEN_PREFIX)
    assert first != second
    # 32 random bytes as base64url — enough entropy that a plain hash is safe.
    assert len(first) > 40


def test_hash_is_stable_and_not_the_secret():
    plaintext = generate_token()
    assert hash_token(plaintext) == hash_token(plaintext)
    assert plaintext not in hash_token(plaintext)


# ── scope ─────────────────────────────────────────────────────


def test_empty_scope_allows_every_use_case():
    assert _token(use_cases=[]).allows("neumann")
    assert _token(use_cases=[]).allows("wiener_linien")


def test_restricted_scope_allows_only_listed_use_cases():
    token = _token(use_cases=["neumann"])
    assert token.allows("neumann")
    assert not token.allows("wiener_linien")


# ── lifecycle ─────────────────────────────────────────────────


def test_revoked_token_is_inactive():
    assert not _token(revoked_at="2026-01-02T00:00:00+00:00").active


def test_expired_token_is_inactive_but_future_expiry_is_fine():
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert not _token(expires_at=past).active
    assert _token(expires_at=future).active


def test_token_without_expiry_stays_active():
    assert _token().active


# ── request validation ────────────────────────────────────────


def test_minimal_request_is_accepted_and_trimmed():
    req = ChatRequest(use_case="neumann", query="  Welches Werkzeug?  ")
    assert req.query == "Welches Werkzeug?"
    # No history given means "replay the session" — distinct from an empty list.
    assert req.history is None
    assert req.session_id is None


def test_empty_query_is_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(use_case="neumann", query="   ")


def test_too_many_images_are_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(
            use_case="neumann",
            query="Was ist das?",
            images=["data:image/png;base64,AAAA"] * 11,
        )


def test_blank_images_are_dropped_and_empty_payloads_rejected():
    req = ChatRequest(
        use_case="neumann",
        query="Was ist das?",
        images=["data:image/png;base64,AAAA", "  "],
    )
    assert req.images == ["data:image/png;base64,AAAA"]

    with pytest.raises(ValidationError):
        ChatRequest(use_case="neumann", query="Was?", images=["data:image/png;base64,"])


def test_bare_base64_image_is_accepted():
    # Not every client bothers with the data-URI wrapper.
    req = ChatRequest(use_case="neumann", query="Was ist das?", images=["AAAABBBB"])
    assert req.images == ["AAAABBBB"]


def test_non_uuid_session_id_is_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(use_case="neumann", query="Was?", session_id="not-a-uuid")


def test_uuid_session_id_is_normalised():
    req = ChatRequest(
        use_case="neumann",
        query="Was?",
        session_id="0FA4A4E0-0000-4000-8000-000000000000",
    )
    assert req.session_id == "0fa4a4e0-0000-4000-8000-000000000000"


def test_explicit_empty_history_is_kept_distinct_from_omitted():
    req = ChatRequest(use_case="neumann", query="Was?", history=[])
    assert req.history == []


# ── job status shaping ────────────────────────────────────────


def test_job_status_hides_the_payload_and_counts_images():
    from core.jobs import job_to_status

    created = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    row = {
        "id": "0fa4a4e0-0000-4000-8000-000000000000",
        "status": "running",
        "use_case": "neumann",
        "session_id": "0fa4a4e0-0000-4000-8000-000000000001",
        "role": "default",
        "query_text": "Was ist das?",
        # asyncpg hands JSONB back as text unless a codec is registered.
        "images": '["data:image/png;base64,AAAA", "AAAA"]',
        "created_at": created,
        "started_at": created,
        "finished_at": None,
        "error": None,
    }
    status = job_to_status(row)

    assert status["request_id"] == row["id"]
    assert status["status"] == "running"
    assert status["image_count"] == 2
    assert status["created_at"] == created.isoformat()
    assert status["finished_at"] is None
    # The images themselves must never ride along on a poll — they are the
    # bulk of the payload and the caller already has them.
    assert "images" not in status


def test_json_field_survives_text_objects_and_garbage():
    from core.jobs import _json_field

    assert _json_field(None, []) == []
    assert _json_field('["a"]', []) == ["a"]
    assert _json_field(["a"], []) == ["a"]
    assert _json_field("not json", []) == []


# ── generated API description ─────────────────────────────────


@pytest.fixture(scope="module")
def public_spec() -> dict:
    """The customer-facing OpenAPI document, built from the real app."""
    from main import app
    from router.public import build_public_spec

    return build_public_spec(app.openapi())


def test_spec_documents_every_public_endpoint_under_its_real_address(public_spec):
    paths = public_spec["paths"]
    assert set(paths) == {
        "/api/v1/me",
        "/api/v1/chat",
        "/api/v1/chat/{request_id}",
        "/api/v1/chat/{request_id}/result",
        "/api/v1/chat/{request_id}/full",
        "/api/v1/sessions/{session_id}",
    }
    # The service-internal prefix must not leak: those addresses are not
    # reachable from outside, so documenting them would mislead every caller.
    assert not any(p.startswith("/v1/") for p in paths)


def test_spec_hides_the_services_internal_endpoints(public_spec):
    joined = " ".join(public_spec["paths"])
    for internal in ("rerank", "evaluate", "ingest-log", "admin", "auth"):
        assert internal not in joined


def test_spec_keeps_referenced_models_and_drops_unrelated_ones(public_spec):
    schemas = public_spec["components"]["schemas"]
    assert "ChatRequest" in schemas
    assert "ChatResult" in schemas
    assert "SessionTranscript" in schemas
    # Internal models of the evaluation service have no business in a
    # customer-facing reference.
    assert "RerankRequest" not in schemas
    assert "EvaluateRequest" not in schemas


def test_spec_declares_bearer_auth_globally(public_spec):
    assert public_spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert public_spec["security"] == [{"bearerAuth": []}]


def test_request_fields_carry_descriptions_for_the_reference(public_spec):
    props = public_spec["components"]["schemas"]["ChatRequest"]["properties"]
    for field in ("use_case", "query", "images", "session_id", "history"):
        assert props[field].get("description"), f"{field} is undocumented"


def test_endpoints_carry_summary_and_documented_failure_modes(public_spec):
    post = public_spec["paths"]["/api/v1/chat"]["post"]
    assert post["summary"] == "Frage stellen"
    assert post["description"].strip()
    for code in ("202", "400", "401", "403"):
        assert code in post["responses"]

    result = public_spec["paths"]["/api/v1/chat/{request_id}/result"]["get"]
    # 409 "not ready yet" is the one an integration must handle to work at all.
    assert "409" in result["responses"]


def test_spec_uses_readable_model_names(public_spec):
    """No FastAPI module-path prefixes in the customer-facing reference.

    FastAPI disambiguates same-named models by prefixing the module path, which
    turns a field's type into `router__public__HistoryMessage` in the rendered
    documentation. Names have to stay distinct across the app for the reference
    to read well.
    """
    for name in public_spec["components"]["schemas"]:
        assert "__" not in name, f"{name} collides with another model"
