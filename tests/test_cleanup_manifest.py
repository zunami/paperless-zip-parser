"""Cleanup safety: only ids from the run manifest may ever be deleted (T15/T16).

These tests stub the HTTP layer, so no Paperless instance is needed. Since the second review round
a recorded id is not enough - the object's name must match too - and ``--apply`` requires a run id.
"""

from __future__ import annotations

import json

import pytest

import verify_paperless as vp


@pytest.fixture(autouse=True)
def _reset_state():
    vp.FAILED.clear()
    yield
    vp.FAILED.clear()


def _point_to(tmp_path, monkeypatch):
    monkeypatch.setattr(vp, "WORK", tmp_path)
    monkeypatch.setattr(vp, "MANIFEST", tmp_path / "run_manifest.json")


def _run(**kinds):
    run = {"created": "now", "documents": [], "tags": [], "correspondents": []}
    run.update(kinds)
    return run


def _manifest(tmp_path, runs: dict, instance: str | None = None) -> None:
    payload = {"instance": vp.BASE if instance is None else instance, "runs": runs}
    (tmp_path / "run_manifest.json").write_text(json.dumps(payload))


def _objects(*, doc="ZIP-Test 11", tag="ZIP-Test-tag", corr="ZIP-Test-correspondent"):
    """What the fake Paperless returns for GET on the recorded ids."""

    def fake(method, path, tok, **kw):
        fake.seen.append((method, path))
        if method == "GET":
            if path.startswith("/api/documents/11/"):
                return 200, {"id": 11, "title": doc}
            if path.startswith("/api/tags/22/"):
                return 200, {"id": 22, "name": tag}
            if path.startswith("/api/correspondents/33/"):
                return 200, {"id": 33, "name": corr}
            return 404, {"detail": "not found"}
        return 204, None

    fake.seen = []
    return fake


def test_cleanup_refuses_without_a_manifest(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        vp.stage_cleanup("token", apply=True, run_id="r1")


def test_cleanup_refuses_an_unreadable_manifest(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    (tmp_path / "run_manifest.json").write_text("{not json")
    with pytest.raises(SystemExit):
        vp.stage_cleanup("token", apply=True, run_id="r1")


def test_apply_without_run_id_is_refused(tmp_path, monkeypatch):
    """A deletion run must name one run; without --run-id nothing may be deleted."""
    _point_to(tmp_path, monkeypatch)
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11, "name": "ZIP-Test 11"}])})
    fake = _objects()
    monkeypatch.setattr(vp, "call", fake)

    with pytest.raises(SystemExit):
        vp.stage_cleanup("token", apply=True)
    assert fake.seen == []


def test_dry_run_deletes_nothing(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11, "name": "ZIP-Test 11"}])})
    fake = _objects()
    monkeypatch.setattr(vp, "call", fake)
    vp.stage_cleanup("token")
    assert fake.seen == []


def test_cleanup_only_touches_recorded_ids(tmp_path, monkeypatch):
    """A foreign document (the review's "ZIP-Test" case) is never a candidate."""
    _point_to(tmp_path, monkeypatch)
    _manifest(tmp_path, {"r1": _run(
        documents=[{"id": 11, "name": "ZIP-Test 11"}],
        tags=[{"id": 22, "name": "ZIP-Test-tag"}],
        correspondents=[{"id": 33, "name": "ZIP-Test-correspondent"}],
    )})
    fake = _objects()
    monkeypatch.setattr(vp, "call", fake)

    vp.stage_cleanup("token", apply=True, run_id="r1")

    deletes = [path for method, path in fake.seen if method == "DELETE"]
    assert deletes == ["/api/documents/11/", "/api/tags/22/", "/api/correspondents/33/"]
    assert all(path.startswith("/api/") for _, path in fake.seen), "no search, only id access"
    assert vp.FAILED == []


def test_identity_mismatch_keeps_the_id(tmp_path, monkeypatch):
    """An id that now belongs to a real document must never be deleted."""
    _point_to(tmp_path, monkeypatch)
    manifest = tmp_path / "run_manifest.json"
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11, "name": "ZIP-Test 11"}])})
    fake = _objects(doc="Steuerbescheid 2025")
    monkeypatch.setattr(vp, "call", fake)

    vp.stage_cleanup("token", apply=True, run_id="r1")

    assert not [p for m, p in fake.seen if m == "DELETE"]
    assert vp.FAILED, "a kept object must be visible"
    kept = json.loads(manifest.read_text())["runs"]["r1"]
    assert kept["documents"] == [{"id": 11, "name": "ZIP-Test 11"}]
    assert kept["cleaned"] is False


def test_entry_without_a_name_is_never_deleted(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11}])})
    fake = _objects()
    monkeypatch.setattr(vp, "call", fake)

    vp.stage_cleanup("token", apply=True, run_id="r1")

    assert not [p for m, p in fake.seen if m == "DELETE"]
    assert vp.FAILED


def test_already_gone_object_is_not_a_failure(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    manifest = tmp_path / "run_manifest.json"
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11, "name": "ZIP-Test 11"}])})
    monkeypatch.setattr(vp, "call", lambda method, path, tok, **kw: (404, {"detail": "gone"}))

    vp.stage_cleanup("token", apply=True, run_id="r1")

    assert vp.FAILED == []
    assert json.loads(manifest.read_text())["runs"]["r1"]["cleaned"] is True


def test_cleanup_reports_http_failures_and_keeps_the_id(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    manifest = tmp_path / "run_manifest.json"
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11, "name": "ZIP-Test 11"}])})

    def fake(method, path, tok, **kw):
        if method == "GET":
            return 200, {"id": 11, "title": "ZIP-Test 11"}
        return 500, {"detail": "boom"}

    monkeypatch.setattr(vp, "call", fake)

    vp.stage_cleanup("token", apply=True, run_id="r1")

    assert vp.FAILED, "a failed delete must be visible"
    kept = json.loads(manifest.read_text())["runs"]["r1"]
    assert kept["documents"] == [{"id": 11, "name": "ZIP-Test 11"}]
    assert kept["cleaned"] is False


def test_cleanup_marks_a_finished_run_and_keeps_the_instance(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    manifest = tmp_path / "run_manifest.json"
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11, "name": "ZIP-Test 11"}])})
    fake = _objects()
    monkeypatch.setattr(vp, "call", fake)

    vp.stage_cleanup("token", apply=True, run_id="r1")

    data = json.loads(manifest.read_text())
    assert data["runs"]["r1"]["documents"] == []
    assert data["runs"]["r1"]["cleaned"] is True
    assert data["instance"] == vp.BASE, "cleanup must not drop the instance tag"


def test_unknown_run_id_is_refused(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    _manifest(tmp_path, {})
    with pytest.raises(SystemExit):
        vp.stage_cleanup("token", apply=True, run_id="does-not-exist")


# --- second review round: instance binding, typed payloads, redirects ------------------


def test_manifest_of_another_instance_is_refused(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    monkeypatch.setattr(vp, "BASE", "https://paperless.example.net")
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11, "name": "ZIP-Test 11"}])},
              instance="https://other.example.net")
    fake = _objects()
    monkeypatch.setattr(vp, "call", fake)

    with pytest.raises(SystemExit):
        vp.stage_cleanup("token", apply=True, run_id="r1")
    assert fake.seen == [], "ids from another instance must never be deleted"


def test_manifest_without_instance_tag_is_refused(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    # An older script version wrote runs but no instance tag - ids could belong elsewhere.
    payload = {"runs": {"r1": _run(documents=[{"id": 11, "name": "ZIP-Test 11"}])}}
    (tmp_path / "run_manifest.json").write_text(json.dumps(payload))
    fake = _objects()
    monkeypatch.setattr(vp, "call", fake)
    with pytest.raises(SystemExit):
        vp.stage_cleanup("token", apply=True, run_id="r1")
    assert fake.seen == []


def test_new_run_records_the_instance(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    monkeypatch.setattr(vp, "BASE", "https://paperless.example.net")
    run_id = vp.start_run()
    data = json.loads((tmp_path / "run_manifest.json").read_text())
    assert data["instance"] == "https://paperless.example.net"
    assert run_id in data["runs"]


def test_record_stores_id_and_name(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    run_id = vp.start_run()
    vp.record(run_id, "documents", 11, "ZIP-Test 11")
    entries = json.loads((tmp_path / "run_manifest.json").read_text())["runs"][run_id]["documents"]
    assert entries == [{"id": 11, "name": "ZIP-Test 11"}]


def test_record_failure_aborts_and_names_the_object(tmp_path, monkeypatch, capsys):
    """Without a manifest entry the object can never be cleaned up - the stage must stop."""
    _point_to(tmp_path, monkeypatch)
    run_id = vp.start_run()

    def broken(_data):
        raise OSError("disk full")

    monkeypatch.setattr(vp, "_save_manifest", broken)
    with pytest.raises(SystemExit):
        vp.record(run_id, "tags", 22, "ZIP-Test-tag")
    out = capsys.readouterr().out
    assert "CLEANUP REQUIRED BY HAND" in out and "22" in out


def test_as_bytes_normalises_error_payloads():
    assert vp.as_bytes(b"PK\x03\x04") == b"PK\x03\x04"
    assert vp.as_bytes(None) == b""
    assert vp.as_bytes({"detail": "boom"}) == b""
    assert vp.as_bytes("text") == b""


def test_redirects_are_refused_not_followed():
    import urllib.error

    handler = vp._NoRedirect()
    req = vp.urllib.request.Request("https://paperless.example.net/api/documents/")
    with pytest.raises(urllib.error.HTTPError) as err:
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.org/")
    assert "refused" in str(err.value)


# --- fourth review round: identity by original file name, record before GET -----------


def test_identity_accepts_the_original_file_name(tmp_path, monkeypatch):
    """A renamed document keeps original_file_name - that alone identifies it."""
    _point_to(tmp_path, monkeypatch)
    manifest = tmp_path / "run_manifest.json"
    name = "zip-parser-test-ab12cd34-0f0f0f.zip"
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11, "name": name}])})

    def fake(method, path, tok, **kw):
        if method == "GET":
            return 200, {"id": 11, "title": "Rechnung 2025", "original_file_name": name}
        return 204, None

    monkeypatch.setattr(vp, "call", fake)
    vp.stage_cleanup("token", apply=True, run_id="r1")
    assert vp.FAILED == []
    assert json.loads(manifest.read_text())["runs"]["r1"]["documents"] == []


def test_identity_accepts_the_title_without_extension(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    _manifest(tmp_path, {"r1": _run(documents=[{"id": 11, "name": "notes.zip"}])})
    monkeypatch.setattr(vp, "call", lambda m, p, t, **kw: (
        (200, {"id": 11, "title": "notes"}) if m == "GET" else (204, None)))
    vp.stage_cleanup("token", apply=True, run_id="r1")
    assert vp.FAILED == []


def _stub_upload(monkeypatch, tmp_path, *, patch_status, title_after, call_log):
    """Drive stage_upload without a server: upload + task succeed, everything else is faked."""
    def fake_upload(path, tok):
        call_log.append(("UPLOAD", path.name))
        return 200, '"task-1"'

    monkeypatch.setattr(vp, "upload", fake_upload)
    monkeypatch.setattr(vp, "wait_task", lambda tid, tok, timeout=120: {
        "status": "success", "result_data": {"document_id": 11}, "related_document_ids": [11]})

    def fake_call(method, path, tok, data=None, headers=None, raw=False):
        call_log.append((method, path))
        if method == "GET" and path == "/api/documents/11/":
            doc = {"id": 11, "title": title_after, "original_file_name": "zip-parser-test.zip",
                   "tags": [22], "correspondent": 33, "mime_type": "application/zip",
                   "content": "x", "archive_serial_number": None, "created": "2026-01-01"}
            return 200, doc
        if method == "PATCH":
            return patch_status, {"detail": "no"} if patch_status != 200 else {"id": 11}
        if method == "POST" and path == "/api/tags/":
            return 201, {"id": 22, "name": "ZIP-Test-tag"}
        if method == "POST" and path == "/api/correspondents/":
            return 201, {"id": 33, "name": "ZIP-Test-Correspondent"}
        if method == "GET" and path.startswith("/api/documents/11/download"):
            return 200, b"PK\x03\x04 rest"
        if method == "GET" and path.startswith("/api/tasks/"):
            return 200, {"status": "success", "result_data": {}, "related_document_ids": []}
        return 200, {}

    monkeypatch.setattr(vp, "call", fake_call)


def test_failed_patch_keeps_the_file_name_as_identity(tmp_path, monkeypatch):
    """A rejected PATCH must not write a title into the manifest that the instance never shows."""
    _point_to(tmp_path, monkeypatch)
    log: list[tuple[str, str]] = []
    _stub_upload(monkeypatch, tmp_path, patch_status=400, title_after="zip-parser-test", call_log=log)
    vp.FAILED.clear()
    vp.stage_upload("token")
    data = json.loads((tmp_path / "run_manifest.json").read_text())
    names = [e["name"] for run in data["runs"].values() for e in run["documents"]]
    assert any(n.startswith("zip-parser-test-") and n.endswith(".zip") for n in names), names
    assert "ZIP-Test 11" not in names, names


def test_document_is_recorded_before_the_first_read_back(tmp_path, monkeypatch):
    """Even if the GET right after creation dies, the id must already be in the manifest."""
    _point_to(tmp_path, monkeypatch)
    log: list[tuple[str, str]] = []
    _stub_upload(monkeypatch, tmp_path, patch_status=200, title_after="ZIP-Test 11", call_log=log)
    inner = vp.call
    seen: dict[str, dict] = {}

    def spy(method, path, tok, data=None, headers=None, raw=False):
        if method == "GET" and path == "/api/documents/11/" and "manifest" not in seen:
            seen["manifest"] = json.loads((tmp_path / "run_manifest.json").read_text())
        return inner(method, path, tok, data=data, headers=headers, raw=raw)

    monkeypatch.setattr(vp, "call", spy)
    vp.FAILED.clear()
    vp.stage_upload("token")

    runs = seen["manifest"]["runs"]
    assert any(run["documents"] for run in runs.values()), "id was not recorded before the GET"


# --- fifth review round: id extraction, honest DOCX verdict ----------------------------


def test_created_ids_unions_both_sources_and_dedupes():
    task = {"status": "success", "result_data": {"document_id": 5},
            "related_document_ids": [5, "6", None, "x"]}
    assert vp.created_ids(task) == [5, 6]


def test_created_ids_tolerates_missing_data():
    assert vp.created_ids(None) == []
    assert vp.created_ids({"status": "failure", "result_data": None}) == []
    assert vp.created_ids({"result_data": {"document_id": None}}) == []


def test_failed_patch_is_a_visible_check(tmp_path, monkeypatch):
    """The PATCH status must not be overwritten by the following GET."""
    _point_to(tmp_path, monkeypatch)
    log: list[tuple[str, str]] = []
    _stub_upload(monkeypatch, tmp_path, patch_status=400, title_after="zip-parser-test", call_log=log)
    vp.FAILED.clear()
    vp.stage_upload("token")
    assert any("metadata PATCH accepted" in f for f in vp.FAILED), vp.FAILED


def test_docx_verdict_does_not_claim_tika(tmp_path, monkeypatch):
    """Five separate DOCX checks, none of them claiming direct Tika evidence."""
    _point_to(tmp_path, monkeypatch)
    log: list[tuple[str, str]] = []
    _stub_upload(monkeypatch, tmp_path, patch_status=200, title_after="ZIP-Test 11", call_log=log)
    vp.FAILED.clear()
    vp.stage_upload("token")
    docx_failures = [f for f in vp.FAILED if "docx" in f]
    assert all("Tika" not in f for f in docx_failures), docx_failures
    assert any("docx: mime type is not application/zip" in f for f in docx_failures), docx_failures


# --- sixth review round: baseline iso, rename only with a successful PATCH ------------


def test_baseline_records_an_unexpectedly_accepted_iso(tmp_path, monkeypatch):
    """Baseline expects 400 for the .iso - if the instance accepts it, the document must be tracked."""
    _point_to(tmp_path, monkeypatch)
    monkeypatch.setattr(vp, "upload", lambda path, tok: (200, '"task-9"'))
    monkeypatch.setattr(vp, "wait_task", lambda tid, tok, timeout=120: {
        "status": "success", "result_data": {"document_id": 44}, "related_document_ids": [44]})
    vp.FAILED.clear()
    vp.stage_baseline("token")
    data = json.loads((tmp_path / "run_manifest.json").read_text())
    names = [e["name"] for run in data["runs"].values() for e in run["documents"]]
    assert any(n.endswith(".iso") for n in names), names
    assert any(n.endswith(".zip") for n in names), names


def test_rename_needs_a_successful_patch(tmp_path, monkeypatch):
    """A 400 on PATCH must not put the new title into the manifest, even if the title is there."""
    _point_to(tmp_path, monkeypatch)
    monkeypatch.setattr(vp, "upload", lambda path, tok: (200, '"task-1"'))
    monkeypatch.setattr(vp, "wait_task", lambda tid, tok, timeout=120: {
        "status": "success", "result_data": {"document_id": 11}, "related_document_ids": [11]})
    state = {"title": "zip-parser-test", "patched_title": None}

    def fake(method, path, tok, data=None, headers=None, raw=False):
        if method == "PATCH":
            state["patched_title"] = json.loads(data)["title"]
            # Simulate the instance applying the title while rejecting the request.
            state["title"] = state["patched_title"]
            return 400, {"detail": "rejected"}
        if method == "GET" and path == "/api/documents/11/":
            return 200, {"id": 11, "title": state["title"],
                         "original_file_name": "zip-parser-test.zip",
                         "tags": [22], "correspondent": 33}
        if method == "POST" and path == "/api/tags/":
            return 201, {"id": 22, "name": "ZIP-Test-tag"}
        if method == "POST" and path == "/api/correspondents/":
            return 201, {"id": 33, "name": "ZIP-Test-Correspondent"}
        return 200, {}

    monkeypatch.setattr(vp, "call", fake)
    vp.FAILED.clear()
    vp.stage_upload("token")

    data = json.loads((tmp_path / "run_manifest.json").read_text())
    names = [e["name"] for run in data["runs"].values() for e in run["documents"]]
    assert state["patched_title"] not in names, names
    assert any(n.startswith("zip-parser-test-") for n in names), names
