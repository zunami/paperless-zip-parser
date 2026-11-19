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
import urllib.request
import uuid
import zipfile
from pathlib import Path

BASE = os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
WORK = Path(__file__).resolve().parent / ".verify"
FAILED: list[str] = []


def token() -> str:
    value = os.environ.get("PAPERLESS_TOKEN", "").strip()
    if not value:
        raise SystemExit("PAPERLESS_TOKEN is not set")
    return value


def call(method: str, path: str, tok: str, data: bytes | None = None,
         headers: dict | None = None, raw: bool = False):
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Token {tok}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return r.status, (body if raw else (json.loads(body) if body else None))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body.decode("utf-8", "replace")
        return e.code, parsed


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILED.append(label)


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


def stage_baseline(tok: str) -> None:
    z = make_zip(f"baseline-{uuid.uuid4().hex[:8]}.zip")
    status, data = upload(z, tok)
    detail = str(data)[:200]
    check("baseline: ZIP is rejected BEFORE the plugin is installed", status == 400, f"HTTP {status} {detail}")
    i = make_iso()
    status, data = upload(i, tok)
    check("baseline: .iso is rejected", status == 400, f"HTTP {status} {str(data)[:120]}")


def stage_upload(tok: str) -> None:
    z = make_zip(f"zip-parser-test-{uuid.uuid4().hex[:8]}.zip")
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
    doc_id = (task.get("result_data") or {}).get("document_id") or (
        (task.get("related_document_ids") or [None])[0]
    )
    if not doc_id:
        print("  ! no document id in the task:", task)
        return

    status, doc = call("GET", f"/api/documents/{doc_id}/", tok)
    check("document exists", status == 200 and doc.get("id") == int(doc_id))
    check("MIME is application/zip", doc.get("mime_type") == "application/zip", doc.get("mime_type"))
    # Since 1.3.0 requires_pdf_rendition is False on purpose: no archive version, so the
    # plain download returns the original ZIP. An archive present here means an old release.
    check("no archive version (1.3.0: original stays original)",
          not doc.get("archived_file_name"), str(doc.get("archived_file_name")))
    check("full text names the file", z.name in (doc.get("content") or ""), (doc.get("content") or "")[:80])

    # The point of 1.3.0: the plain download endpoint hands out the ZIP, not a wrapper PDF.
    status, plain = call("GET", f"/api/documents/{doc_id}/download/", tok, raw=True)
    check("plain download returns the ZIP", status == 200 and plain[:2] == b"PK",
          f"HTTP {status} magic={plain[:4]!r}")
    check("plain download byte-identical (sha256)", hashlib.sha256(plain).hexdigest() == digest,
          hashlib.sha256(plain).hexdigest()[:16])

    status, blob = call("GET", f"/api/documents/{doc_id}/download/?original=true", tok, raw=True)
    check("original downloadable", status == 200, f"HTTP {status}")
    check("original byte-identical (sha256)", hashlib.sha256(blob).hexdigest() == digest,
          hashlib.sha256(blob).hexdigest()[:16])

    # Metadata: create tag + correspondent, set them, read back
    tag_name = f"ZIP-Test-{uuid.uuid4().hex[:6]}"
    status, tag = call("POST", "/api/tags/", tok, json.dumps({"name": tag_name}).encode(),
                       {"Content-Type": "application/json"})
    corr_name = f"ZIP-Test-Correspondent-{uuid.uuid4().hex[:6]}"
    status2, corr = call("POST", "/api/correspondents/", tok,
                         json.dumps({"name": corr_name}).encode(), {"Content-Type": "application/json"})
    if status == 201 and status2 == 201:
        patch = {"tags": [tag["id"]], "correspondent": corr["id"], "title": f"ZIP-Test {doc_id}"}
        status, _ = call("PATCH", f"/api/documents/{doc_id}/", tok, json.dumps(patch).encode(),
                         {"Content-Type": "application/json"})
        status, doc2 = call("GET", f"/api/documents/{doc_id}/", tok)
        check("tag + correspondent + title persist",
              doc2.get("tags") == [tag["id"]] and doc2.get("correspondent") == corr["id"]
              and doc2.get("title") == f"ZIP-Test {doc_id}",
              f"tags={doc2.get('tags')} corr={doc2.get('correspondent')} title={doc2.get('title')}")
        call("DELETE", f"/api/tags/{tag['id']}/", tok)
        call("DELETE", f"/api/correspondents/{corr['id']}/", tok)
    else:
        check("tag/correspondent creatable", False, f"HTTP {status}/{status2}")

    # Negative probe: ZIP only, no other types
    status, data = upload(make_iso(), tok)
    if status == 400:
        check("negative: .iso rejected (upload)", True, f"HTTP {status}")
    else:
        t = wait_task(str(data).strip('"'), tok, timeout=120)
        check("negative: .iso is not archived (consumer rejects)",
              t.get("status", "").lower() == "failure" and not t.get("related_document_ids"),
              f"{t.get('status')} {str(t.get('result_data'))[:80]}")

    # Neighbours untouched: docx stays docx (no ZIP-parser hijack)
    WORK.mkdir(exist_ok=True)
    docx = WORK / f"probe-{uuid.uuid4().hex[:8]}.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
    status, data = upload(docx, tok)
    detail = str(data)[:160]
    if status in (200, 202):
        tid = str(data).strip('"')
        t = wait_task(tid, tok, timeout=90)
        check("docx is NOT archived as a ZIP",
              t.get("status", "").lower() == "failure" and not t.get("related_document_ids"),
              f"{t.get('status')} {str(t.get('result_data'))[:100]}")
    else:
        check("docx is recognised as docx (not rejected as ZIP)", False, f"HTTP {status} {detail}")

    print(f"\nTEST DOCUMENT ID: {doc_id}")
    (WORK / "last_doc_id").write_text(str(doc_id))


def stage_cleanup(tok: str) -> None:
    marker = WORK / "last_doc_id"
    ids = []
    if marker.is_file():
        ids.append(marker.read_text().strip())
    # every document that looks like a test document
    status, data = call("GET", "/api/documents/?query=ZIP-Archiv&page_size=50", tok)
    if status == 200:
        ids += [str(d["id"]) for d in data.get("results", []) if "test" in d.get("title", "").lower()
                or "zip-parser" in (d.get("content") or "")]
    for doc_id in sorted(set(ids)):
        status, _ = call("DELETE", f"/api/documents/{doc_id}/", tok)
        print(f"  document {doc_id} moved to trash (HTTP {status})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["baseline", "upload", "cleanup"])
    args = ap.parse_args()
    tok = token()
    {"baseline": stage_baseline, "upload": stage_upload, "cleanup": stage_cleanup}[args.stage](tok)
    print()
    if FAILED:
        print(f"{len(FAILED)} test(s) failed: {FAILED}")
        return 1
    print("All tests of this stage passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
