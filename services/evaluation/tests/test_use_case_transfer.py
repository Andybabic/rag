"""Use Case exportieren und auf einer anderen (oder derselben) Instanz importieren."""

from __future__ import annotations

import asyncio
import json
import zipfile

import httpx
import pytest
from config import settings
from core import transfer
from core.transfer import (
    ImportPlan,
    Rename,
    TargetState,
    decode_vector,
    encode_vector,
    plan_import,
    strip_secrets,
    transform_rows,
)
from main import app

# ── Umbenennen ──────────────────────────────────────────────────────────────


def test_collections_follow_the_new_id():
    r = Rename(old="neumann", new="neumann_test")
    assert r.collection("neumann_machines") == "neumann_test_machines"
    assert r.collection("mem_neumann") == "mem_neumann_test"
    # Not named after the id: prefixed, so the one new prefix still finds it.
    assert r.collection("gw_cnc_steps") == "neumann_test_gw_cnc_steps"
    assert r.prefixes(["gw_", "x_"]) == ["neumann_test_"]


def test_same_id_changes_nothing():
    r = Rename(old="ustp", new="ustp")
    assert r.collection("ustp_default") == "ustp_default"
    assert r.prefixes(["ustp_"]) == ["ustp_"]
    payload = {"use_case": "ustp", "stored_path": "ustp/ab/x.pdf"}
    assert r.value(payload) == payload


def test_references_inside_payloads_are_rewritten():
    r = Rename(old="wl", new="wl_neu", collections={"wl_default": "wl_neu_default"})
    payload = {
        "use_case": "wl",
        "stored_path": "wl/ab/abc_Handbuch.pdf",
        "collection": "wl_default",
        "images": [{"url": "/api/images/wl/img_1"}],
        "text": "wl bleibt wl",
    }
    assert r.value(payload) == {
        "use_case": "wl_neu",
        "stored_path": "wl_neu/ab/abc_Handbuch.pdf",
        "collection": "wl_neu_default",
        "images": [{"url": "/api/images/wl_neu/img_1"}],
        "text": "wl bleibt wl",
    }


def test_vectors_survive_the_archive_exactly():
    vector = [0.1, -2.5, 3.0e-7, 1234.5678]
    as_float32 = decode_vector(encode_vector(vector))
    assert decode_vector(encode_vector(as_float32)) == as_float32
    assert as_float32 == pytest.approx(vector, rel=1e-6)


def test_secrets_are_dropped_but_named():
    db = {
        "usecase_config": [{"openai_api_key_encrypted": "gAAA…", "ollama_api_key_encrypted": None,
                            "llm_model": "qwen"}],
        "usecase_skills": [{"name": "wetter", "variables": {"API_KEY": "geheim", "CITY": "Wien"}}],
    }
    omitted = strip_secrets(db)
    assert db["usecase_config"][0]["openai_api_key_encrypted"] is None
    assert db["usecase_config"][0]["llm_model"] == "qwen"
    assert db["usecase_skills"][0]["variables"] == {"API_KEY": "", "CITY": ""}
    assert omitted == {"config_keys": ["openai_api_key"],
                       "skill_variables": {"wetter": ["API_KEY", "CITY"]}}
    assert "geheim" not in json.dumps(db)


# ── Konfliktprüfung ─────────────────────────────────────────────────────────


def _manifest(**uc) -> dict:
    return {
        "use_case": {"id": "neumann", "slug": "neumann", "label": "Neumann",
                     "default_collection": "neumann_machines",
                     "collection_prefixes": ["neumann_"],
                     "agent_action_names": ["SEARCH"], **uc},
        "collections": [
            {"name": "neumann_machines", "points": 3, "dimension": 4, "embed_models": ["bge-m3"]},
            {"name": "mem_neumann", "points": 1, "dimension": 4, "embed_models": ["bge-m3"]},
        ],
        "omitted": {},
    }


def _state(**kw) -> TargetState:
    base = dict(use_cases={}, collections=set(), files_present=False,
                embed_model="bge-m3", embed_dimension=None, users={"admin"})
    return TargetState(**{**base, **kw})


def test_fresh_instance_takes_the_use_case_as_is():
    plan = plan_import(_manifest(), None, "neumann", None, _state())
    assert plan.problems == []
    assert plan.slug == "neumann"
    assert plan.rename.collections == {"neumann_machines": "neumann_machines",
                                       "mem_neumann": "mem_neumann"}


def test_existing_id_demands_a_new_name():
    state = _state(use_cases={"neumann": {"slug": "neumann", "prefixes": ["neumann_"]}},
                   collections={"neumann_machines", "mem_neumann"})
    plan = plan_import(_manifest(), None, "neumann", None, state)
    assert any("neuen Namen" in p for p in plan.problems)

    clone = plan_import(_manifest(), None, "neumann2", "Neumann Kopie", state)
    assert clone.problems == []
    assert clone.slug == "neumann2"
    assert clone.label == "Neumann Kopie"
    assert clone.default_collection == "neumann2_machines"
    assert clone.prefixes == ["neumann2_"]


def test_clone_named_under_the_originals_prefix_is_refused():
    # "neumann_kopie_machines" starts with "neumann_": the original would
    # search the clone's documents as its own.
    state = _state(use_cases={"neumann": {"slug": "neumann", "prefixes": ["neumann_"]}})
    plan = plan_import(_manifest(), None, "neumann_kopie", None, state)
    assert any("Präfix „neumann_“" in p for p in plan.problems)


def test_new_name_must_not_fall_under_another_prefix():
    # "gw_test_*" would be searched by gw_stpoelten (prefix "gw_").
    state = _state(use_cases={"gw_stpoelten": {"slug": "gw-stpoelten", "prefixes": ["gw_"]}})
    plan = plan_import(_manifest(), None, "gw_test", None, state)
    assert any("gw_stpoelten" in p for p in plan.problems)


def test_foreign_collections_under_the_new_prefix_are_a_clash():
    plan = plan_import(_manifest(), None, "neumann", None,
                       _state(collections={"neumann_altlast"}))
    assert any("neumann_altlast" in p for p in plan.problems)


def test_leftover_files_block_the_id():
    plan = plan_import(_manifest(), None, "neumann", None, _state(files_present=True))
    assert any("Dateiablage" in p for p in plan.problems)


def test_other_embedding_model_is_refused():
    plan = plan_import(_manifest(), None, "neumann", None, _state(embed_model="nomic-embed-text"))
    assert any("Embedding-Modell" in p for p in plan.problems)
    # The use case's own config wins over the instance default …
    ok = plan_import(_manifest(), {"embedding_model": "bge-m3"}, "neumann", None,
                     _state(embed_model="nomic-embed-text"))
    assert ok.problems == []
    # … and Ollama's ":latest" is the same model.
    same = plan_import(_manifest(), None, "neumann", None, _state(embed_model="bge-m3:latest"))
    assert same.problems == []


def test_invalid_id_is_refused():
    plan = plan_import(_manifest(), None, "Neu Mann", None, _state())
    assert any("Ungültige ID" in p for p in plan.problems)


def test_warnings_name_what_still_needs_attention():
    manifest = _manifest(agent_action_names=["SEARCH", "SEARCH_CNC"])
    manifest["omitted"] = {"config_keys": ["openai_api_key"],
                           "skill_variables": {"wetter": ["API_KEY"]}}
    plan = plan_import(manifest, None, "neumann_2", None, _state(), users=["admin", "gast"])
    text = " ".join(plan.warnings)
    assert "SEARCH_CNC" in text
    assert "openai_api_key" in text
    assert "API_KEY" in text
    assert "gast" in text and "admin" not in text


# ── Zeilen umschreiben ──────────────────────────────────────────────────────


def _plan(old="neumann", new="neumann_2") -> ImportPlan:
    r = Rename(old=old, new=new)
    r.collections = {"neumann_machines": r.collection("neumann_machines")}
    return ImportPlan(rename=r, slug=new.replace("_", "-"), label="Neu",
                      prefixes=r.prefixes(["neumann_"]),
                      default_collection=r.collection("neumann_machines"),
                      problems=[], warnings=[])


def test_rows_get_new_ids_and_keep_their_links():
    q = "11111111-1111-1111-1111-111111111111"
    s = "22222222-2222-2222-2222-222222222222"
    db = {
        "use_cases": [{"id": "neumann", "slug": "neumann", "label": "Neumann",
                       "default_collection": "neumann_machines",
                       "collection_prefixes": ["neumann_"]}],
        "queries": [{"id": q, "use_case": "neumann", "session_id": s,
                     "citations": [{"stored_path": "neumann/ab/x.pdf",
                                    "collection": "neumann_machines"}]}],
        "feedback": [{"id": "33333333-3333-3333-3333-333333333333", "query_id": q,
                      "rating": "positive"}],
        "ingestion_log": [{"id": "44444444-4444-4444-4444-444444444444", "use_case": "neumann",
                           "file_hash": "abc", "stored_path": "neumann/ab/x.pdf",
                           "collection": "neumann_machines"}],
        "usecase_skills": [{"id": "55555555-5555-5555-5555-555555555555",
                            "use_case": "neumann", "name": "wetter"}],
        "usecase_skill_functions": [{"id": "66666666-6666-6666-6666-666666666666",
                                     "skill_id": "55555555-5555-5555-5555-555555555555",
                                     "name": "aktuell"}],
    }
    out = transform_rows(db, _plan())

    uc = out["use_cases"][0]
    assert uc["id"] == "neumann_2" and uc["slug"] == "neumann-2"
    assert uc["default_collection"] == "neumann_2_machines"
    assert uc["collection_prefixes"] == ["neumann_2_"]

    query = out["queries"][0]
    assert query["id"] != q and query["session_id"] != s
    assert query["use_case"] == "neumann_2"
    assert query["citations"] == [{"stored_path": "neumann_2/ab/x.pdf",
                                   "collection": "neumann_2_machines"}]
    assert out["feedback"][0]["query_id"] == query["id"]

    doc = out["ingestion_log"][0]
    assert doc["stored_path"] == "neumann_2/ab/x.pdf"
    assert doc["collection"] == "neumann_2_machines"
    assert doc["file_hash"] == "abc"

    assert out["usecase_skill_functions"][0]["skill_id"] == out["usecase_skills"][0]["id"]
    # Source rows are untouched — the archive stays reusable.
    assert db["queries"][0]["id"] == q


# ── Export → Import über Fake-Dienste ───────────────────────────────────────


class FakeServices:
    """vectordb + cleaning in memory, reachable through httpx.MockTransport."""

    def __init__(self):
        self.collections: dict[str, dict] = {}  # name -> {"dimension", "points": {id: point}}
        self.files: dict[tuple[str, str], bytes] = {}
        self.fail_file_put = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/collections":
            return httpx.Response(200, json={"collections": [{"name": n} for n in self.collections]})
        if path == "/v1/points/export":
            body = json.loads(request.content)
            col = self.collections[body["collection"]]
            points = list(col["points"].values())
            start = body.get("offset") or 0
            page = points[start:start + body["limit"]]
            nxt = start + body["limit"] if start + body["limit"] < len(points) else None
            return httpx.Response(200, json={"dimension": col["dimension"], "points": page, "next": nxt})
        if path == "/v1/points/import":
            body = json.loads(request.content)
            if body["create"]:
                if body["collection"] in self.collections:
                    return httpx.Response(409, json={"error": "collection_exists"})
                self.collections[body["collection"]] = {"dimension": body["dimension"], "points": {}}
            for p in body["points"]:
                self.collections[body["collection"]]["points"][p["id"]] = p
            return httpx.Response(200, json={"imported": len(body["points"])})
        if path.startswith("/v1/collection/") and request.method == "DELETE":
            self.collections.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(200, json={"status": "ok"})
        if path.startswith("/v1/transfer/"):
            parts = path.split("/", 5)  # ['', 'v1', 'transfer', uc, kind, rel]
            uc = parts[3]
            if request.method == "DELETE":
                for key in [k for k in self.files if k[0] == uc]:
                    del self.files[key]
                return httpx.Response(200, json={"status": "ok"})
            if parts[4] == "files":
                return httpx.Response(200, json={"files": [
                    {"path": rel, "size": len(data)} for (u, rel), data in sorted(self.files.items()) if u == uc
                ]})
            rel = parts[5]
            if request.method == "GET":
                return httpx.Response(200, content=self.files[(uc, rel)])
            if self.fail_file_put:
                return httpx.Response(500, json={"error": "disk_full"})
            self.files[(uc, rel)] = request.read()
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)


@pytest.fixture()
def services(monkeypatch, tmp_path):
    fake = FakeServices()
    fake.collections = {
        "neumann_machines": {"dimension": 4, "points": {
            f"00000000-0000-0000-0000-00000000000{i}": {
                "id": f"00000000-0000-0000-0000-00000000000{i}",
                "vector": [0.5 * i, 0.25, -1.0, 2.0],
                "payload": {"use_case": "neumann", "stored_path": "neumann/ab/x.pdf",
                            "_embed_model": "bge-m3", "text": f"chunk {i}"},
            } for i in range(5)
        }},
        "mem_neumann": {"dimension": 4, "points": {}},
        "gw_cnc_steps": {"dimension": 4, "points": {}},
    }
    fake.files = {("neumann", "ab/abcdef_x.pdf"): b"%PDF-1.7 original",
                  ("neumann", "ab/images/img_ab_p001_i00.png"): b"\x89PNG"}
    monkeypatch.setattr(settings, "TRANSFER_DIR", str(tmp_path))
    monkeypatch.setattr(transfer, "_client", lambda timeout=0: httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler), base_url="http://fake"))
    monkeypatch.setattr(settings, "VECTORDB_SERVICE_URL", "http://fake")
    monkeypatch.setattr(settings, "CLEANING_SERVICE_URL", "http://fake")

    db = {
        "use_cases": [{"id": "neumann", "slug": "neumann", "label": "Neumann",
                       "default_collection": "neumann_machines",
                       "collection_prefixes": ["neumann_"], "agent_action_names": ["SEARCH"]}],
        "usecase_config": [{"use_case": "neumann", "openai_api_key_encrypted": "secret"}],
        "usecase_skills": [], "queries": [], "user_use_cases": [{"username": "admin"}],
    }

    async def dump(use_case):
        return json.loads(json.dumps(db))

    written: dict = {}

    async def write(db_rows, plan):
        written["db"], written["plan"] = db_rows, plan
        return {"use_cases": 1}

    async def state(target_id):
        uses = {"neumann": {"slug": "neumann", "prefixes": ["neumann_"]}}
        files_present = any(u == target_id for u, _ in fake.files)
        return TargetState(use_cases=uses, collections=set(fake.collections),
                           files_present=files_present, embed_model="bge-m3",
                           embed_dimension=None, users={"admin"})

    async def refresh():
        return 1

    monkeypatch.setattr(transfer, "dump_database", dump)
    monkeypatch.setattr(transfer, "write_database", write)
    monkeypatch.setattr(transfer, "target_state", state)
    monkeypatch.setattr("core.use_cases.refresh_use_cases", refresh)
    fake.written = written
    return fake


async def _wait(job):
    for _ in range(200):
        if job.status != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("job hängt")


async def _export_and_upload(services) -> str:
    job = await _wait(transfer.start_export("neumann"))
    assert job.status == "done", job.error
    upload_id = transfer.new_upload()
    data = job.file.read_bytes()
    offset = 0
    for i in range(0, len(data), 1000):  # several chunks, like the dashboard
        offset = transfer.append_upload(upload_id, offset, data[i:i + 1000])
    return upload_id


@pytest.mark.anyio
async def test_export_holds_everything_but_secrets(services):
    job = await _wait(transfer.start_export("neumann"))
    assert job.status == "done", job.error
    with zipfile.ZipFile(job.file) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        db = json.loads(zf.read("db.json"))
    assert {"vectors/neumann_machines.jsonl", "vectors/mem_neumann.jsonl",
            "files/ab/abcdef_x.pdf", "files/ab/images/img_ab_p001_i00.png"} <= names
    # Another use case's collection is not part of this one.
    assert "vectors/gw_cnc_steps.jsonl" not in names
    assert manifest["collections"][1]["points"] == 5
    assert manifest["collections"][1]["embed_models"] == ["bge-m3"]
    assert manifest["omitted"]["config_keys"] == ["openai_api_key"]
    assert "secret" not in json.dumps(db)
    assert job.result["file_name"].startswith("neumann-")


@pytest.mark.anyio
async def test_clone_under_a_new_name_lands_completely(services):
    upload_id = await _export_and_upload(services)
    manifest, plan = await transfer.prepare_import(upload_id, "kopie", None)
    assert plan.problems == []
    job = await _wait(transfer.start_import(upload_id, manifest, plan))
    assert job.status == "done", job.error

    copied = services.collections["kopie_machines"]["points"]
    original = services.collections["neumann_machines"]["points"]
    assert len(copied) == 5
    for pid, p in original.items():
        assert copied[pid]["vector"] == pytest.approx(p["vector"])
        assert copied[pid]["payload"]["use_case"] == "kopie"
        assert copied[pid]["payload"]["stored_path"] == "kopie/ab/x.pdf"
    assert "mem_kopie" in services.collections
    assert services.files[("kopie", "ab/abcdef_x.pdf")] == b"%PDF-1.7 original"
    assert services.written["plan"].rename.new == "kopie"
    assert job.result["collections"]["neumann_machines"] == "kopie_machines"
    # The upload is consumed.
    with pytest.raises(transfer.TransferError):
        transfer.read_manifest(upload_id)


@pytest.mark.anyio
async def test_failed_import_leaves_nothing_behind(services):
    upload_id = await _export_and_upload(services)
    manifest, plan = await transfer.prepare_import(upload_id, "kopie", None)
    services.fail_file_put = True
    job = await _wait(transfer.start_import(upload_id, manifest, plan))
    assert job.status == "error"
    assert not any(n.startswith("kopie") or n == "mem_kopie" for n in services.collections)
    assert not any(u == "kopie" for u, _ in services.files)
    assert "db" not in services.written


@pytest.mark.anyio
async def test_retried_chunk_is_not_appended_twice(services):
    upload_id = transfer.new_upload()
    transfer.append_upload(upload_id, 0, b"abc")
    with pytest.raises(transfer.TransferError):
        transfer.append_upload(upload_id, 0, b"abc")


@pytest.mark.anyio
async def test_random_zip_is_not_accepted(services, tmp_path):
    upload_id = transfer.new_upload()
    buf = tmp_path / "x.zip"
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "etwas-anderes"}))
    transfer.append_upload(upload_id, 0, buf.read_bytes())
    with pytest.raises(transfer.TransferError, match="kein Kermit"):
        transfer.read_manifest(upload_id)


# ── HTTP ────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_import_under_a_taken_id_is_refused_over_http(services):
    upload_id = await _export_and_upload(services)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with client:
        inspect = (await client.post(f"/v1/admin/transfer/uploads/{upload_id}/inspect")).json()
        assert inspect["exists"] is True
        assert inspect["manifest"]["use_case"]["id"] == "neumann"
        assert inspect["problems"]

        resp = await client.post(f"/v1/admin/transfer/uploads/{upload_id}/import",
                                 json={"target_id": "neumann"})
        assert resp.status_code == 409
        assert any("neuen Namen" in p for p in resp.json()["detail"]["problems"])

        resp = await client.post(f"/v1/admin/transfer/uploads/{upload_id}/import",
                                 json={"target_id": "neumann2"})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        for _ in range(200):
            status = (await client.get(f"/v1/admin/transfer/jobs/{job_id}")).json()
            if status["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert status["status"] == "done", status["error"]
