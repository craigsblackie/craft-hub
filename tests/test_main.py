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
    c = TestClient(main.app, headers={'X-Requested-With': 'crafthub'})
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
    c = TestClient(main.app, headers={'X-Requested-With': 'crafthub'})
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
    c = TestClient(main.app, headers={'X-Requested-With': 'crafthub'})
    assert c.get("/api/export").json()["apps"][0]["version"] == "v1.0.0"
    main.jobs["x"] = {"state": "error", "msg": "boom", "pct": 0, "action": "install"}
    c.post("/api/apps/x/dismiss")
    assert "x" not in main.jobs


def test_csrf_protection():
    c = TestClient(main.app)  # no custom header
    assert c.post("/api/refresh").status_code == 403
    assert c.delete("/api/apps/x").status_code == 403
    ok = {"X-Requested-With": "crafthub"}
    assert c.post("/api/apps/nope/dismiss", headers=ok).status_code == 200
    assert c.post("/api/apps/nope/dismiss", headers={**ok, "Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/apps/nope/dismiss", headers={**ok, "Origin": "http://testserver"}).status_code == 200
    assert c.post("/api/apps/nope/dismiss", headers={**ok, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.get("/api/health").status_code == 200  # reads are unaffected


def test_security_headers_and_csp():
    c = TestClient(main.app)
    r = c.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"] and "unsafe-inline'; img" in r.headers["content-security-policy"]
    assert "'unsafe-inline'" not in r.headers["content-security-policy"].split("script-src")[1].split(";")[0]
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "<script>" not in r.text and 'onclick="' not in r.text  # nothing inline for the CSP to block
    assert c.get("/ui/app.js").status_code == 200 and c.get("/ui/../main.py").status_code == 404


def test_apps_do_not_get_dashboard_csp(env):
    asyncio.run(main.do_install("x", "v1.0.0"))
    assert "content-security-policy" not in TestClient(main.app).get("/app/x/").headers


def test_github_token_never_in_shared_client_headers():
    assert "Authorization" not in main.UA


def test_release_parsing_rejects_untrusted_assets():
    def rel(url, name="x-web-1.zip"):
        return {"tag_name": "v1", "published_at": "2020-01-01T00:00:00Z", "html_url": "", "draft": False, "prerelease": False,
                "assets": [{"name": name, "browser_download_url": url, "size": 1}]}
    good = "https://github.com/o/r/releases/download/v1/x-web-1.zip"
    assert len(main.parse_releases([rel(good)])) == 1
    assert len(main.parse_releases([rel("https://evil.example/x-web-1.zip")])) == 0
    assert len(main.parse_releases([rel("http://github.com/x-web-1.zip")])) == 0
    assert len(main.parse_releases([rel(good, "../../x-web-1.zip")])) == 0


def test_settings_validation(env):
    c = TestClient(main.app, headers={"X-Requested-With": "crafthub"})
    assert c.post("/api/settings", json={"check_hours": "abc"}).status_code == 422
    assert c.post("/api/settings", json={"check_hours": 99999}).status_code == 422
    assert c.post("/api/settings", json={"check_hours": 2}).status_code == 200


def test_download_size_cap(env, monkeypatch):
    monkeypatch.setattr(main, "MAX_UNPACKED", 100)
    ok = asyncio.run(main.do_install("x", "v1.0.0"))
    assert ok is False and "size limit" in main.jobs["x"]["msg"]
    assert not list((main.PACKAGES / "x").glob("*.zip"))
