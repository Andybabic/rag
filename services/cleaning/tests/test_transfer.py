"""Use-Case-Transfer: Dateiablage eines Use Case lesen und schreiben."""

from __future__ import annotations

import httpx
import pytest
from config import settings
from main import app


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_round_trip_of_originals_and_images(client, storage):
    (storage / "alt" / "ab" / "images").mkdir(parents=True)
    (storage / "alt" / "ab" / "abcdef_Handbuch.pdf").write_bytes(b"%PDF-1")
    (storage / "alt" / "ab" / "images" / "img_ab_p001_i00.png").write_bytes(b"png")

    listing = (await client.get("/v1/transfer/alt/files")).json()["files"]
    assert [f["path"] for f in listing] == [
        "ab/abcdef_Handbuch.pdf",
        "ab/images/img_ab_p001_i00.png",
    ]

    for entry in listing:
        data = (await client.get(f"/v1/transfer/alt/file/{entry['path']}")).content
        resp = await client.put(f"/v1/transfer/neu/file/{entry['path']}", content=data)
        assert resp.status_code == 200 and resp.json()["size"] == entry["size"]

    assert (storage / "neu" / "ab" / "abcdef_Handbuch.pdf").read_bytes() == b"%PDF-1"
    assert not list(storage.rglob("*.part"))


@pytest.mark.anyio
async def test_unknown_use_case_folder_lists_empty(client, storage):
    assert (await client.get("/v1/transfer/leer/files")).json()["files"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["../andere/x.pdf", "ab/../../andere/x.pdf", "%2Fetc%2Fpasswd"])
async def test_archive_paths_cannot_leave_the_use_case(client, storage, path):
    resp = await client.put(f"/v1/transfer/neu/file/{path}", content=b"x")
    assert resp.status_code in (400, 404)
    assert not (storage / "andere").exists()
    assert list(storage.rglob("x.pdf")) == []


@pytest.mark.anyio
async def test_invalid_use_case_id_is_refused(client, storage):
    resp = await client.get("/v1/transfer/..%2F..%2Ftmp/files")
    assert resp.status_code in (400, 404)


@pytest.mark.anyio
async def test_rollback_removes_the_whole_folder(client, storage):
    (storage / "neu" / "ab").mkdir(parents=True)
    (storage / "neu" / "ab" / "a.pdf").write_bytes(b"1")
    (storage / "alt").mkdir()
    resp = await client.delete("/v1/transfer/neu")
    assert resp.json()["removed"] == 1
    assert not (storage / "neu").exists()
    assert (storage / "alt").exists()
