#!/usr/bin/env python3
"""Verification against a real Paperless instance (API only, no docker exec).

Stages:
  baseline  - upload a ZIP and an .iso: both must be rejected (before the plugin is installed)
  upload    - upload a ZIP, check task, document, hash, metadata and negative probes
  cleanup   - move test documents to the trash

Credentials come from PAPERLESS_URL and PAPERLESS_TOKEN (environment, never in the repo).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os

import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

BASE = os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")
WORK = Path(__file__).resolve().parent / ".verify"
MANIFEST = WORK / "run_manifest.json"
FAILED: list[str] = []


def token() -> str:
    value = os.environ.get("PAPERLESS_TOKEN", "").strip()
    if not value:
        raise SystemExit("PAPERLESS_TOKEN is not set")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect: urlopen would resend the Authorization header to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect to {newurl} refused (token protection)", headers, fp
        )


_OPENER = urllib.request.build_opener(_NoRedirect)


def call(method: str, path: str, tok: str, data: bytes | None = None,
         headers: dict | None = None, raw: bool = False):
    """HTTP call with a stable return type: (status, dict|str) or (status, bytes|None).

    With raw=True the payload is always bytes on success; on an error status it is None, so a
    caller can never slice a JSON error body as if it were file content.
    """
    # Plain HTTP is only for a trusted local instance; anything remote must be HTTPS so the
    # API token never travels in clear text.
    parsed = urllib.parse.urlsplit(BASE)
    if parsed.scheme not in ("http", "https"):
        raise SystemExit(f"PAPERLESS_URL must be http(s), got {parsed.scheme!r}")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS \
            and os.environ.get("PAPERLESS_ALLOW_HTTP", "") != "1":
        raise SystemExit(
            f"refusing plain HTTP to {parsed.hostname} - use https:// or set PAPERLESS_ALLOW_HTTP=1"
        )
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Token {tok}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=120) as r:
            body = r.read()
            if raw:
                return r.status, body
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read()
        if raw:
            return e.code, None
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body.decode("utf-8", "replace")


def as_bytes(payload) -> bytes:
    """Body of a raw call as bytes. An error or empty body becomes b"" so checks fail cleanly
    instead of raising TypeError/KeyError somewhere down the line."""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    return b""


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILED.append(label)


def _load_manifest() -> dict:
    """Ids this script created. Anything not listed here is never touched.

    The manifest also carries the instance it was written against: ids are only meaningful in
    that database, so a manifest from another PAPERLESS_URL must not be used for deletions here.
    """
    if not MANIFEST.is_file():
        return {"runs": {}}
    try:
        data = json.loads(MANIFEST.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"manifest {MANIFEST} is unreadable ({exc}) - refusing to continue")
    if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
        raise SystemExit(f"manifest {MANIFEST} has an unexpected shape - refusing to continue")
    instance = data.get("instance")
    if data["runs"] and instance is None:
        raise SystemExit(
            f"manifest {MANIFEST} records runs but no instance - it was written by an older "
            "script version. Inspect it by hand, then delete or replace it"
        )
    if instance is not None and instance != BASE:
        raise SystemExit(
            f"manifest {MANIFEST} belongs to {instance}, but PAPERLESS_URL is {BASE}. "
            "Document ids are instance-specific - refusing to delete anything here"
        )
    return data


def _save_manifest(data: dict) -> None:
    WORK.mkdir(exist_ok=True)
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(MANIFEST)


def start_run() -> str:
    """Open a new manifest run so cleanup knows exactly which ids belong to this run."""
    data = _load_manifest()
    run_id = uuid.uuid4().hex[:12]
    data["instance"] = BASE
    data["runs"][run_id] = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "documents": [],
        "tags": [],
        "correspondents": [],
    }
    _save_manifest(data)
    print(f"run id: {run_id} ({BASE})")
    return run_id


def record(run_id: str, kind: str, value, name: str) -> None:
    """Register a created object with the name that identifies it, before anything else happens.

    A failure here aborts the stage: an object without a manifest entry can never be cleaned up,
    so continuing would leave test data behind silently.
    """
    try:
        data = _load_manifest()
        run = data["runs"].setdefault(
            run_id, {"created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "documents": [], "tags": [], "correspondents": []}
        )
        entry = {"id": int(value), "name": name}
        entries = run.setdefault(kind, [])
        if entry not in entries:
            entries.append(entry)
        _save_manifest(data)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - without an entry the object can not be cleaned up
        print(f"  ! CLEANUP REQUIRED BY HAND: {kind[:-1]} {value} ({name}) - manifest not saved: {exc}")
        raise SystemExit(f"aborting: {kind[:-1]} {value} ({name}) is not in {MANIFEST}") from exc


def rename(run_id: str, kind: str, value, name: str) -> None:
    """Keep the manifest name in sync after the object was renamed in Paperless."""
    data = _load_manifest()
    for entry in data["runs"].get(run_id, {}).get(kind, []):
        if entry.get("id") == int(value):
            entry["name"] = name
    _save_manifest(data)


def make_zip(name: str) -> Path:
    WORK.mkdir(exist_ok=True)
    path = WORK / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", f"Test archive for the ZIP parser, {uuid.uuid4()}\n")
        zf.writestr("notes/2026.txt", "Content stays inside the archive, it is not indexed.\n" * 20)
    return path


def make_iso() -> Path:
    WORK.mkdir(exist_ok=True)
    path = WORK / f"negative-{uuid.uuid4().hex[:8]}.iso"
    block = b"\x00" * 32768 + b"CD001" + b"\x00" * 2048
    path.write_bytes(block * 24)
    return path


def make_docx(name: str | None = None) -> Path:
    """A minimal but genuinely valid DOCX (OOXML package), so Tika has something real to parse."""
    WORK.mkdir(exist_ok=True)
    path = WORK / (name or f"probe-{uuid.uuid4().hex[:8]}.docx")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>DOCX probe {uuid.uuid4().hex[:8]}</w:t></w:r></w:p></w:body>'
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return path


def upload(path: Path, tok: str):
    boundary = "----paperless-zip-parser" + uuid.uuid4().hex
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'.encode()
    )
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(path.read_bytes())
    body.write(f"\r\n--{boundary}--\r\n".encode())
    return call(
        "POST", "/api/documents/post_document/", tok, data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def wait_task(task_id: str, tok: str, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, data = call("GET", f"/api/tasks/?task_id={task_id}", tok)
        if status == 200 and data:
            rows = data.get("results") if isinstance(data, dict) else data
            last = rows[0] if rows else {}
            if last.get("status", "").lower() in ("success", "failure"):
                return last
        time.sleep(3)
    return last


def created_ids(task) -> list[int]:
    """Every document id a task produced.

    Paperless reports the new document either in ``result_data.document_id`` or in
    ``related_document_ids`` - depending on version and parser. Checking only one of them is how a
    created document escapes the manifest.
    """
    if not isinstance(task, dict):
        return []
    result = task.get("result_data")
    first = result.get("document_id") if isinstance(result, dict) else None
    ids: list[int] = []
    for value in [first, *(task.get("related_document_ids") or [])]:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value not in ids:
            ids.append(value)
    return ids


def stage_baseline(tok: str) -> None:
    run_id = start_run()
    z = make_zip(f"baseline-{uuid.uuid4().hex[:8]}.zip")
    status, data = upload(z, tok)
    detail = str(data)[:200]
    if status in (200, 202):
        # Unexpected: wait for the task and record whatever document it created, so cleanup
        # can remove it later. Nothing is recorded when the upload is rejected as expected.
        task = wait_task(str(data).strip('"'), tok, timeout=120)
        for created in created_ids(task):
            record(run_id, "documents", created, z.name)
    check("baseline: ZIP is rejected BEFORE the plugin is installed", status == 400, f"HTTP {status} {detail}")
    i = make_iso()
    status, data = upload(i, tok)
    if status in (200, 202):
        # Same rule as above: an accepted upload has a task, and its documents must be recorded.
        task = wait_task(str(data).strip('"'), tok, timeout=120)
        for created in created_ids(task):
            record(run_id, "documents", created, i.name)
    check("baseline: .iso is rejected", status == 400, f"HTTP {status} {str(data)[:120]}")


def stage_upload(tok: str) -> None:
    run_id = start_run()
    token = run_id[:8]  # part of every name, so cleanup can verify what it deletes
    z = make_zip(f"zip-parser-test-{token}-{uuid.uuid4().hex[:6]}.zip")
    digest = hashlib.sha256(z.read_bytes()).hexdigest()
    status, data = upload(z, tok)
    check("upload accepted", status in (200, 202), f"HTTP {status} {str(data)[:200]}")
    if status not in (200, 202):
        return
    task_id = data if isinstance(data, str) else data.get("task_id") or str(data)
    task = wait_task(str(task_id).strip('"'), tok)
    ok_task = task.get("status", "").lower() == "success"
    check("consumer task succeeded", ok_task,
          f"{task.get('status')} {str(task.get('result_data'))[:120]}")
    ids = created_ids(task)
    doc_id = ids[0] if ids else None
    if doc_id is None:
        print("  ! no document id in the task:", task)
        return

    # Registered before any further request: if this GET fails, the id is already in the manifest.
    # The name is the uploaded file name, which Paperless keeps as original_file_name.
    for created in ids:
        record(run_id, "documents", created, z.name)
    status, doc = call("GET", f"/api/documents/{doc_id}/", tok)
    check("document exists", status == 200 and doc.get("id") == int(doc_id))
    check("MIME is application/zip", doc.get("mime_type") == "application/zip", doc.get("mime_type"))
    # Since 1.3.0 requires_pdf_rendition is False on purpose: no archive version, so the
    # plain download returns the original ZIP. An archive present here means an old release.
    check("no archive version (1.3.0: original stays original)",
          not doc.get("archived_file_name"), str(doc.get("archived_file_name")))
    check("full text names the file", z.name in (doc.get("content") or ""), (doc.get("content") or "")[:80])

    # The point of 1.3.0: the plain download endpoint hands out the ZIP, not a wrapper PDF.
    status, payload = call("GET", f"/api/documents/{doc_id}/download/", tok, raw=True)
    plain = as_bytes(payload)
    check("plain download returns the ZIP", status == 200 and plain[:2] == b"PK",
          f"HTTP {status} bytes={len(plain)} magic={plain[:4]!r}")
    # A hash is only meaningful for a body that actually arrived.
    plain_hash = hashlib.sha256(plain).hexdigest() if plain else None
    check("plain download byte-identical (sha256)", plain_hash == digest,
          (plain_hash or "no body")[:16])

    status, payload = call("GET", f"/api/documents/{doc_id}/download/?original=true", tok, raw=True)
    blob = as_bytes(payload)
    check("original downloadable", status == 200 and bool(blob), f"HTTP {status} bytes={len(blob)}")
    blob_hash = hashlib.sha256(blob).hexdigest() if blob else None
    check("original byte-identical (sha256)", blob_hash == digest, (blob_hash or "no body")[:16])

    # Metadata: create tag + correspondent, set them, read back. Both are registered right after
    # creation and stay in the manifest - cleanup removes them, this stage does not.
    tag_name = f"ZIP-Test-{token}-{uuid.uuid4().hex[:6]}"
    status, tag = call("POST", "/api/tags/", tok, json.dumps({"name": tag_name}).encode(),
                       {"Content-Type": "application/json"})
    if status == 201:
        record(run_id, "tags", tag["id"], tag.get("name") or tag_name)
    corr_name = f"ZIP-Test-Correspondent-{token}-{uuid.uuid4().hex[:6]}"
    status2, corr = call("POST", "/api/correspondents/", tok,
                         json.dumps({"name": corr_name}).encode(), {"Content-Type": "application/json"})
    if status2 == 201:
        record(run_id, "correspondents", corr["id"], corr.get("name") or corr_name)
    if status == 201 and status2 == 201:
        wanted = f"ZIP-Test {token} {doc_id}"
        patch = {"tags": [tag["id"]], "correspondent": corr["id"], "title": wanted}
        patch_status, patch_body = call("PATCH", f"/api/documents/{doc_id}/", tok,
                                        json.dumps(patch).encode(), {"Content-Type": "application/json"})
        # Separate names: overwriting patch_status with the GET result hides a rejected PATCH.
        get_status, doc2 = call("GET", f"/api/documents/{doc_id}/", tok)
        doc2 = doc2 if isinstance(doc2, dict) else {}
        check("metadata PATCH accepted", patch_status in (200, 204) and get_status == 200,
              f"PATCH HTTP {patch_status} GET HTTP {get_status} {str(patch_body)[:80]}")
        check("tag + correspondent + title persist",
              doc2.get("tags") == [tag["id"]] and doc2.get("correspondent") == corr["id"]
              and doc2.get("title") == f"ZIP-Test {token} {doc_id}",
              f"tags={doc2.get('tags')} corr={doc2.get('correspondent')} title={doc2.get('title')}")
        # Cleanup compares names: follow only if the instance really shows the new title.
        # Without this guard a rejected PATCH would leave a name that matches nothing.
        if patch_status in (200, 204) and doc2.get("title") == wanted:
            rename(run_id, "documents", doc_id, wanted)
    else:
        check("tag/correspondent creatable", False, f"HTTP {status}/{status2}")

    # Negative probe: ZIP only, no other types
    i = make_iso()
    status, data = upload(i, tok)
    if status == 400:
        check("negative: .iso rejected (upload)", True, f"HTTP {status}")
    else:
        t = wait_task(str(data).strip('"'), tok, timeout=120)
        for created in created_ids(t):
            record(run_id, "documents", created, i.name)
        check("negative: .iso is not archived (consumer rejects)",
              t.get("status", "").lower() == "failure" and not t.get("related_document_ids"),
              f"{t.get('status')} {str(t.get('result_data'))[:80]}")

    # Neighbours untouched: a real DOCX must be parsed by Tika, never by this plugin.
    docx = make_docx(f"docx-probe-{token}-{uuid.uuid4().hex[:6]}.docx")
    status, data = upload(docx, tok)
    detail = str(data)[:160]
    if status in (200, 202):
        t = wait_task(str(data).strip('"'), tok, timeout=90)
        ids = created_ids(t)
        # Same rule as for the main document: register every id first, then read back.
        for created in ids:
            record(run_id, "documents", created, docx.name)
        get_status, d = call("GET", f"/api/documents/{ids[0]}/", tok) if ids else (0, None)
        mime = d.get("mime_type") if isinstance(d, dict) else None
        task_ok = t.get("status", "").lower() == "success"
        # Four separate conditions instead of one merged verdict:
        check("docx: consumer task succeeded", task_ok, f"{t.get('status')}")
        check("docx: document readable", get_status == 200 and isinstance(d, dict),
              f"HTTP {get_status}")
        check("docx: document id confirmed", bool(ids) and (d or {}).get("id") == ids[0],
              f"ids={ids} id={(d or {}).get('id')}")
        # A DOCX claimed by this plugin would be stored as application/zip.
        check("docx: mime type is not application/zip", mime != "application/zip", f"mime={mime}")
        # The label names what was actually shown: not a direct Tika log.
        check("docx: import succeeded via a non-ZIP parser", task_ok and bool(ids) and mime not in (None, "application/zip"),
              f"mime={mime} task={t.get('status')}")
    else:
        check("docx: upload accepted by a parser", False, f"HTTP {status} {detail}")

    print(f"\nTEST DOCUMENT ID: {doc_id}")


def _matches(obj, expected: str) -> bool:
    """Is this really the object the manifest recorded?

    A document keeps its uploaded file name as ``original_file_name`` even after a title change, so
    a name mismatch against the title alone is not proof of a different object.
    """
    if not isinstance(obj, dict) or not expected:
        return False
    title = str(obj.get("title") or "")
    original = str(obj.get("original_file_name") or "")
    name = str(obj.get("name") or "")
    if expected in (title, original, name):
        return True
    return bool(title) and Path(expected).stem == title


def stage_cleanup(tok: str, *, apply: bool = False, run_id: str | None = None) -> None:
    """Delete only what this script created, and only with --apply --run-id.

    The manifest written by the upload stage is the single source of truth, and even a recorded id
    is not enough: the object's current name must still match the recorded one. An id that was
    reused for a real document is therefore never deleted.
    """
    if not MANIFEST.is_file():
        raise SystemExit(
            f"no run manifest at {MANIFEST} - refusing to delete anything "
            "(run the upload stage first, or clean up by hand)"
        )
    data = _load_manifest()
    runs = data["runs"]
    if apply and run_id is None:
        raise SystemExit(
            "cleanup --apply needs --run-id <id> (printed by the upload stage) - "
            "a deletion run must name exactly one run"
        )
    if run_id is not None:
        if run_id not in runs:
            raise SystemExit(f"run {run_id} is not in {MANIFEST}")
        selected = {run_id: runs[run_id]}
    else:
        selected = {rid: run for rid, run in runs.items() if not run.get("cleaned")}
    if not selected:
        print("nothing to clean up: every recorded run is already cleaned")
        return

    print("APPLY - deleting recorded ids only" if apply else "DRY RUN - pass --apply --run-id to delete")
    for rid, run in sorted(selected.items()):
        print(f"run {rid} (created {run.get('created', '?')})")
        for kind in ("documents", "tags", "correspondents"):
            for entry in run.get(kind, []):
                print(f"  {kind[:-1]} {entry['id']} ({entry.get('name', '?')})"
                      f": {'deleted' if apply else 'would be deleted'}")
    if not apply:
        return

    failures = 0
    for rid, run in sorted(selected.items()):
        for kind in ("documents", "tags", "correspondents"):
            remaining = []
            for entry in run.get(kind, []):
                obj_id, expected = entry["id"], entry.get("name") or ""
                status, obj = call("GET", f"/api/{kind}/{obj_id}/", tok)
                if status == 404:
                    print(f"  {kind[:-1]} {obj_id}: already gone (HTTP 404)")
                    continue
                if status != 200:
                    failures += 1
                    print(f"  {kind[:-1]} {obj_id}: could not verify (HTTP {status}) - kept")
                    remaining.append(entry)
                    continue
                actual = str(obj.get("title") or obj.get("name") or "") if isinstance(obj, dict) else ""
                if not _matches(obj, expected):
                    failures += 1
                    print(f"  {kind[:-1]} {obj_id}: identity mismatch "
                          f"(expected {expected!r}, found {actual!r}) - NOT deleted")
                    remaining.append(entry)
                    continue
                status, body = call("DELETE", f"/api/{kind}/{obj_id}/", tok)
                if status in (200, 204):
                    print(f"  {kind[:-1]} {obj_id} ({actual}): deleted (HTTP {status})")
                elif status == 404:
                    print(f"  {kind[:-1]} {obj_id}: already gone (HTTP 404)")
                else:
                    failures += 1
                    print(f"  {kind[:-1]} {obj_id}: FAILED (HTTP {status}) {str(body)[:160]}")
                    remaining.append(entry)
            run[kind] = remaining
        run["cleaned"] = not any(run.get(k) for k in ("documents", "tags", "correspondents"))
        _save_manifest(data)
    check("cleanup: every recorded object removed", failures == 0, f"{failures} kept/failed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["baseline", "upload", "cleanup"])
    ap.add_argument("--apply", action="store_true",
                    help="cleanup only: actually delete the recorded ids (default is a dry run)")
    ap.add_argument("--run-id", default=None,
                    help="cleanup only: restrict to one run from the manifest")
    args = ap.parse_args()
    if args.stage == "cleanup":
        stage_cleanup(token(), apply=args.apply, run_id=args.run_id)
    else:
        {"baseline": stage_baseline, "upload": stage_upload}[args.stage](token())
    print()
    if FAILED:
        print(f"{len(FAILED)} test(s) failed: {FAILED}")
        return 1
    print("All tests of this stage passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
