import asyncio
import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


SITE = {"x-web-1.0/index.html": "<html>hi</html>" * 200, "x-web-1.0/x-abcdef0123456789.js": "var a=1;" * 400,
        "x-web-1.0/x-abcdef0123456789_bg.wasm": b"\0asm" + b"\1" * 5000}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "data")
    monkeypatch.setattr(main, "APPS", tmp_path / "data" / "apps")
    monkeypatch.setattr(main, "TMP", tmp_path / "data" / ".tmp")
    monkeypatch.setattr(main, "PACKAGES", tmp_path / "pkgs")
    monkeypatch.setattr(main, "STATE_FILE", tmp_path / "data" / "state.json")
    main.state.update(catalog={}, installed={}, log=[], notified={}, checked_at=0)
    main.jobs.clear()
    main.locks.clear()
    main.auto_failed.clear()
    zips = {"v1.0.0": make_zip(SITE), "v1.1.0": make_zip(SITE)}

    def handler(req: httpx.Request):
        path = req.url.path
        if path.endswith("SHA256SUMS.txt"):
            tag = path.split("/")[-2]
            return httpx.Response(200, text=f"{hashlib.sha256(zips[tag]).hexdigest()}  x-web-{tag}.zip\n")
        tag = path.split("/")[-2]
        return httpx.Response(200, content=zips[tag])

    main.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rels = [{"tag": t, "published": "2020-01-01T00:00:00Z", "url": "", "body": "", "asset": f"x-web-{t}.zip", "size": len(zips[t]),
             "asset_url": f"https://example.test/{t}/x-web-{t}.zip", "sums_url": f"https://example.test/{t}/SHA256SUMS.txt"} for t in ("v1.1.0", "v1.0.0")]
    main.state["catalog"]["x"] = {"name": "x", "description": "", "repo": "", "releases": rels}
    return zips


def test_vkey_orders_versions():
    assert main.vkey("v0.10.0") > main.vkey("v0.9.1") > main.vkey("0.2")


def test_age_hours():
    assert main.age_hours("2020-01-01T00:00:00Z") > 1000
    assert main.age_hours("garbage") > 1000


def test_safe_extract_rejects_traversal(tmp_path):
    z = zipfile.ZipFile(io.BytesIO(make_zip({"../evil.txt": "x"})))
    with pytest.raises(RuntimeError, match="unsafe"):
        main.safe_extract(z, tmp_path / "d")


def test_safe_extract_rejects_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "MAX_UNPACKED", 1000)
    z = zipfile.ZipFile(io.BytesIO(make_zip({"a": "x" * 5000})))
    with pytest.raises(RuntimeError, match="unpacks"):
        main.safe_extract(z, tmp_path / "d")


def test_install_upgrade_rollback_delete(env):
    asyncio.run(main.do_install("x", "v1.0.0"))
    assert main.state["installed"]["x"]["version"] == "v1.0.0"
    assert main.state["installed"]["x"]["verified"] is True
    assert (main.PACKAGES / "x" / "x-web-v1.0.0.zip").exists()
    asyncio.run(main.do_install("x", "v1.1.0", "update"))
    rec = main.state["installed"]["x"]
    assert rec["version"] == "v1.1.0" and rec["previous"] == "v1.0.0"
    c = TestClient(main.app)
    assert c.post("/api/apps/x/rollback").status_code == 200
    assert main.state["installed"]["x"]["version"] == "v1.0.0"
    assert c.delete("/api/apps/x").status_code == 200
    assert not (main.APPS / "x").exists()


def test_checksum_mismatch_is_rejected(env, monkeypatch):
    env["v1.0.0"] = b"tampered"  # served bytes no longer match the checksum computed earlier
    good = hashlib.sha256(make_zip(SITE)).hexdigest()
    orig = main.client

    def handler(req):
        if req.url.path.endswith("SHA256SUMS.txt"):
            return httpx.Response(200, text=f"{good}  x-web-v1.0.0.zip\n")
        return httpx.Response(200, content=b"tampered")

    main.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = asyncio.run(main.do_install("x", "v1.0.0"))
    assert ok is False and "x" not in main.state["installed"]
    assert not (main.PACKAGES / "x" / "x-web-v1.0.0.zip").exists()
    assert main.jobs["x"]["state"] == "error"


def test_failed_activation_restores_current(tmp_path, monkeypatch):
    base = tmp_path / "app"
    (base / "current").mkdir(parents=True)
    (base / "current" / "f").write_text("old")
    monkeypatch.setattr(main.shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        main.activate(tmp_path / "new", base)
    assert (base / "current" / "f").read_text() == "old"


def test_serving_headers_gzip_head_and_traversal(env):
    asyncio.run(main.do_install("x", "v1.0.0"))
    c = TestClient(main.app)
    r = c.get("/app/x/x-abcdef0123456789_bg.wasm", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/wasm"
    assert r.headers["content-encoding"] == "gzip"
    assert "immutable" in r.headers["cache-control"]
    assert c.get("/app/x/").headers["cache-control"] == "no-cache"
    assert c.head("/app/x/").status_code == 200
    assert c.get("/app/x/..%2f..%2fstate.json").status_code == 404
    assert c.get("/app/nope/").status_code == 404
    assert c.get("/app/x", follow_redirects=False).status_code == 307


def test_auto_update_holds_fresh_releases(env, monkeypatch):
    asyncio.run(main.do_install("x", "v1.0.0"))
    main.state["installed"]["x"]["auto_update"] = True
    from datetime import datetime, timezone
    main.state["catalog"]["x"]["releases"][0]["published"] = datetime.now(timezone.utc).isoformat()
    asyncio.run(main.auto_update())
    assert main.state["installed"]["x"]["version"] == "v1.0.0"  # held back: released just now
    main.state["catalog"]["x"]["releases"][0]["published"] = "2020-01-01T00:00:00Z"
    asyncio.run(main.auto_update())
    assert main.state["installed"]["x"]["version"] == "v1.1.0"


def test_export_and_dismiss(env):
    asyncio.run(main.do_install("x", "v1.0.0"))
    c = TestClient(main.app)
    assert c.get("/api/export").json()["apps"][0]["version"] == "v1.0.0"
    main.jobs["x"] = {"state": "error", "msg": "boom", "pct": 0, "action": "install"}
    c.post("/api/apps/x/dismiss")
    assert "x" not in main.jobs
