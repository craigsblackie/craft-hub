"""CraftHub: install, update and host the storytold *craft WebAssembly apps."""
import asyncio
import gzip
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

OWNER = os.getenv("GH_OWNER", "storytold")
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
EXCLUDE = {x.strip() for x in os.getenv("EXCLUDE_REPOS", "artcraft").split(",") if x.strip()}
EXTRA = [x.strip() for x in os.getenv("EXTRA_REPOS", "").split(",") if x.strip()]
REPO_RE = re.compile(os.getenv("REPO_PATTERN", r"^[a-z]+craft$"))
DATA = Path(os.getenv("DATA_DIR", "/data"))
APPS = DATA / "apps"
PACKAGES = Path(os.getenv("PACKAGES_DIR", "/packages"))
KEEP = int(os.getenv("KEEP_PACKAGES", "3"))
TMP = DATA / ".tmp"
MAX_UNPACKED = int(os.getenv("MAX_UNPACKED_MB", "500")) * 1024 * 1024
MAX_FILES = 5000
FRESH_HOURS = float(os.getenv("MIN_RELEASE_AGE_HOURS", "24"))
NOTIFY_URL = os.getenv("NOTIFY_URL", "").strip()
STATE_FILE = DATA / "state.json"
UI = Path(__file__).parent / "ui"
UA = {"User-Agent": "crafthub/1.0", "Accept": "application/vnd.github+json"}
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
SAFE_ASSET = re.compile(r"^[A-Za-z0-9._-]+$")

REPLACES = {
    "photocraft": "Adobe Photoshop", "lightcraft": "Adobe Lightroom", "vectorcraft": "Adobe Illustrator",
    "filmcraft": "Adobe Premiere Pro", "pdfcraft": "Adobe Acrobat", "effectcraft": "Adobe After Effects",
    "designcraft": "Adobe InDesign", "soundcraft": "Avid Pro Tools", "wordcraft": "Microsoft Word",
    "gridcraft": "Microsoft Excel", "deckcraft": "Microsoft PowerPoint", "cadcraft": "AutoCAD",
}

state = {"catalog": {}, "installed": {}, "checked_at": 0, "settings": {"check_hours": 6.0, "auto_update_all": False}, "log": [], "notified": {}}
jobs: dict = {}
problems: dict = {}
auto_flags: dict = {}
auto_failed: dict = {}
locks: dict = {}
client: httpx.AsyncClient


def save():
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def log(msg, level="info"):
    state["log"] = ([{"t": int(time.time()), "level": level, "msg": msg}] + state["log"])[:200]
    print(f"[{level}] {msg}", flush=True)
    save()


def vkey(tag):
    return tuple(int(x) for x in re.findall(r"\d+", tag or "")) or (0,)


def age_hours(published):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(published.replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:
        return 1e9


async def notify(title, msg):
    if not NOTIFY_URL:
        return
    try:
        await client.post(NOTIFY_URL, content=msg.encode(), headers={"Title": title, "Content-Type": "text/plain"})
    except Exception as e:
        print(f"[warn] notification failed: {e}", flush=True)


# ---------- GitHub catalog ----------
async def gh(url, etag=None):
    h = {"If-None-Match": etag} if etag else {}
    if TOKEN:  # the token is only ever sent to the GitHub API, never to downloads or notification URLs
        h["Authorization"] = f"Bearer {TOKEN}"
    r = await client.get(url, headers=h)
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise RuntimeError("GitHub API rate limit hit (set GITHUB_TOKEN to raise it)")
    return r


def trusted_url(u):
    p = urlparse(u or "")
    return p.scheme == "https" and bool(p.hostname) and (p.hostname == "github.com" or p.hostname.endswith(".githubusercontent.com"))


def parse_releases(rels):
    out = []
    for rel in rels:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        web = next((a for a in rel["assets"] if re.search(r"-web-.*\.zip$", a["name"])), None)
        if not web or not SAFE_ASSET.match(web["name"]) or not trusted_url(web["browser_download_url"]):
            continue
        sums = next((a for a in rel["assets"] if a["name"].upper().startswith("SHA256SUMS")), None)
        out.append({"tag": rel["tag_name"], "published": rel["published_at"], "url": rel["html_url"], "body": (rel.get("body") or "")[:1500],
                    "asset": web["name"], "asset_url": web["browser_download_url"], "size": web["size"],
                    "sums_url": sums["browser_download_url"] if sums and trusted_url(sums["browser_download_url"]) else None})
    return sorted(out, key=lambda r: vkey(r["tag"]), reverse=True)


async def refresh_catalog():
    try:
        r = await gh(f"https://api.github.com/users/{OWNER}/repos?per_page=100")
        r.raise_for_status()
        repos = [x for x in r.json() if REPO_RE.match(x["name"]) and x["name"] not in EXCLUDE and not x["archived"]]
        names = {x["name"]: x for x in repos}
        for n in EXTRA:
            if n not in names:
                rr = await gh(f"https://api.github.com/repos/{OWNER}/{n}")
                if rr.status_code == 200:
                    names[n] = rr.json()
        cat = state["catalog"]
        problems.clear()
        for n, repo in names.items():
            old = cat.get(n, {})
            try:
                rr = await gh(f"https://api.github.com/repos/{OWNER}/{n}/releases?per_page=15", old.get("etag"))
                if rr.status_code == 304:
                    rels = old.get("releases", [])
                elif rr.status_code == 200:
                    rels = parse_releases(rr.json())
                else:
                    raise RuntimeError(f"GitHub returned {rr.status_code}")
            except Exception as e:
                problems[n] = str(e)
                continue
            cat[n] = {"name": n, "description": repo.get("description") or "", "repo": repo["html_url"], "releases": rels,
                      "etag": rr.headers.get("etag") or old.get("etag"), "pushed": repo.get("pushed_at")}
        for n in list(cat):
            if n not in names:
                del cat[n]
        state["checked_at"] = int(time.time())
        save()
        return True
    except Exception as e:
        log(f"Catalog refresh failed: {e}", "error")
        return False


# ---------- install / delete ----------
def app_dir(name):
    return APPS / name


def dir_size(p: Path):
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def precompress(root: Path):
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in (".wasm", ".js", ".html", ".css", ".json", ".svg") and f.stat().st_size > 1024:
            with open(f, "rb") as s, gzip.open(str(f) + ".gz", "wb", compresslevel=9) as d:
                shutil.copyfileobj(s, d)


def safe_extract(z: zipfile.ZipFile, dest: Path):
    dest = dest.resolve()
    infos = z.infolist()
    if len(infos) > MAX_FILES:
        raise RuntimeError(f"package has too many files ({len(infos)})")
    if sum(m.file_size for m in infos) > MAX_UNPACKED:
        raise RuntimeError(f"package unpacks to more than {MAX_UNPACKED // 1048576} MB")
    for m in infos:
        t = (dest / m.filename).resolve()
        if dest not in t.parents and t != dest:
            raise RuntimeError(f"unsafe path in zip: {m.filename}")
    z.extractall(dest)


def sha256_file(p: Path):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stage_package(zpath: Path, stage: Path) -> Path:
    """Extract, locate the site root and pre-compress. Blocking: run in a thread."""
    with zipfile.ZipFile(zpath) as z:
        safe_extract(z, stage)
    idx = next(stage.rglob("index.html"), None)
    if not idx:
        raise RuntimeError("package has no index.html")
    precompress(idx.parent)
    return idx.parent


def activate(root: Path, base: Path):
    """Swap `root` in as current, keeping the old current as previous. Restores on failure."""
    cur, prev, old_prev = base / "current", base / "previous", base / "previous.old"
    base.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(old_prev, ignore_errors=True)
    if prev.exists():
        prev.rename(old_prev)
    try:
        if cur.exists():
            cur.rename(prev)
        shutil.move(str(root), str(cur))
    except Exception:
        if not cur.exists() and prev.exists():
            prev.rename(cur)
        if old_prev.exists() and not prev.exists():
            old_prev.rename(prev)
        raise
    shutil.rmtree(old_prev, ignore_errors=True)


async def do_install(name, version=None, action="install"):
    lock = locks.setdefault(name, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "operation already in progress")
    async with lock:
        job = jobs[name] = {"action": action, "state": "running", "pct": 0, "msg": "Starting"}
        try:
            entry = state["catalog"].get(name)
            if not entry or not entry["releases"]:
                raise RuntimeError("no web release available")
            rel = next((r for r in entry["releases"] if r["tag"] == version), None) if version else entry["releases"][0]
            if not rel:
                raise RuntimeError(f"version {version} not found")
            TMP.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=TMP))
            try:
                pkgdir = PACKAGES / name
                zpath = pkgdir / rel["asset"]
                if zpath.exists():
                    job["msg"], job["pct"] = f"Using stored package {rel['tag']}", 40
                    digest = await asyncio.to_thread(sha256_file, zpath)
                else:
                    pkgdir.mkdir(parents=True, exist_ok=True)
                    part = zpath.with_suffix(".part")
                    h = hashlib.sha256()
                    job["msg"] = f"Downloading {rel['tag']}"
                    async with client.stream("GET", rel["asset_url"]) as r:
                        r.raise_for_status()
                        total = int(r.headers.get("content-length") or rel["size"] or 0)
                        got = 0
                        with open(part, "wb") as f:
                            async for chunk in r.aiter_bytes(262144):
                                f.write(chunk)
                                h.update(chunk)
                                got += len(chunk)
                                if got > MAX_UNPACKED:
                                    raise RuntimeError("download exceeds the size limit")
                                job["pct"] = int(got / total * 80) if total else 40
                    part.replace(zpath)
                    digest = h.hexdigest()
                verified = False
                if rel["sums_url"]:
                    job["msg"] = "Verifying checksum"
                    sr = await client.get(rel["sums_url"])
                    for line in sr.text.splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and parts[-1].lstrip("*") == rel["asset"]:
                            if parts[0].lower() != digest:
                                zpath.unlink(missing_ok=True)
                                raise RuntimeError("SHA256 mismatch, package discarded")
                            verified = True
                job["msg"], job["pct"] = "Extracting and optimising", 85
                root = await asyncio.to_thread(stage_package, zpath, tmp / "stage")
                job["msg"], job["pct"] = "Activating", 97
                base = app_dir(name)
                old = state["installed"].get(name)
                await asyncio.to_thread(activate, root, base)
                rec = {"version": rel["tag"], "installed_at": int(time.time()), "size": await asyncio.to_thread(dir_size, base / "current"),
                       "verified": verified, "auto_update": (old or {}).get("auto_update", auto_flags.pop(name, False)), "previous": old["version"] if old else None}
                state["installed"][name] = rec
                for old_pkg in sorted(pkgdir.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)[KEEP:]:
                    old_pkg.unlink(missing_ok=True)
                log(f"{dict(install='Installed', update='Updated')[action]} {name} {rel['tag']}" + (" (checksum verified)" if verified else ""))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            job.update(state="done", pct=100, msg="Done")
        except Exception as e:
            job.update(state="error", msg=str(e) or type(e).__name__)
            log(f"{action} {name} failed: {job['msg']}", "error")
            await notify(f"CraftHub: {name} failed", f"{action} failed: {job['msg']}")
            return False
        return True


def cleanup_job_later(name):
    async def _c():
        await asyncio.sleep(8)
        j = jobs.get(name)
        if j and j["state"] == "done":
            jobs.pop(name, None)
    asyncio.create_task(_c())


def start(name, coro):
    async def run():
        try:
            await coro
        except HTTPException:
            pass
        cleanup_job_later(name)
    asyncio.create_task(run())


def latest_of(name):
    rels = state["catalog"].get(name, {}).get("releases") or []
    return rels[0]["tag"] if rels else None


async def check_notifications():
    for name, rec in state["installed"].items():
        lt = latest_of(name)
        if lt and vkey(lt) > vkey(rec["version"]) and state["notified"].get(name) != lt:
            state["notified"][name] = lt
            save()
            await notify(f"CraftHub: {name} {lt} available", f"{name} {rec['version']} -> {lt}")


async def auto_update():
    for name, rec in list(state["installed"].items()):
        rels = state["catalog"].get(name, {}).get("releases") or []
        lt = rels[0]["tag"] if rels else None
        if not (lt and vkey(lt) > vkey(rec["version"]) and (rec.get("auto_update") or state["settings"]["auto_update_all"])):
            continue
        if auto_failed.get(name) == lt:
            continue
        if age_hours(rels[0]["published"]) < FRESH_HOURS:
            continue  # hold brand-new releases back until they are FRESH_HOURS old
        log(f"Auto-updating {name} {rec['version']} -> {lt}")
        if await do_install(name, lt, "update"):
            await notify(f"CraftHub: {name} updated", f"{name} {rec['version']} -> {lt}")
        else:
            auto_failed[name] = lt


async def background():
    await asyncio.sleep(2)
    while True:
        try:
            if time.time() - state["checked_at"] > state["settings"]["check_hours"] * 3600 or not state["catalog"]:
                await refresh_catalog()
            await check_notifications()
            await auto_update()
        except Exception as e:
            log(f"Background task error: {e}", "error")
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app):
    global client
    DATA.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    for part in PACKAGES.glob("*/*.part"):
        part.unlink(missing_ok=True)
    # Unraid template variables are authoritative when set
    if os.getenv("CHECK_INTERVAL_HOURS"):
        state["settings"]["check_hours"] = max(0.25, float(os.environ["CHECK_INTERVAL_HOURS"]))
    if os.getenv("AUTO_UPDATE_ALL"):
        state["settings"]["auto_update_all"] = os.environ["AUTO_UPDATE_ALL"].lower() in ("1", "true", "yes", "on")
    client = httpx.AsyncClient(headers=UA, follow_redirects=True, timeout=httpx.Timeout(60, read=300))
    t = asyncio.create_task(background())
    yield
    t.cancel()
    await client.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
if ALLOWED_HOSTS:  # defends against DNS-rebinding; empty = accept any Host
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS + ["127.0.0.1", "localhost"])


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and request.method not in ("GET", "HEAD"):
        # CSRF: a cross-site page can send a "simple" POST/DELETE without any preflight, so require a
        # custom header (forces a CORS preflight, which we never grant) plus a same-origin check.
        if request.headers.get("x-requested-with") != "crafthub":
            return JSONResponse({"detail": "missing X-Requested-With header"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
    resp = await call_next(request)
    if not path.startswith("/app/"):  # hosted apps keep their own (permissive, WASM-friendly) policy
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if path == "/" or path.startswith("/ui/"):
            resp.headers["Content-Security-Policy"] = CSP
    return resp


def view(name):
    c = state["catalog"].get(name, {})
    rec = state["installed"].get(name)
    lt = latest_of(name)
    return {"name": name, "replaces": REPLACES.get(name, ""), "description": c.get("description", ""), "repo": c.get("repo"),
            "latest": lt, "latest_published": (c.get("releases") or [{}])[0].get("published"), "latest_size": (c.get("releases") or [{}])[0].get("size"),
            "latest_notes": (c.get("releases") or [{}])[0].get("body"), "latest_url": (c.get("releases") or [{}])[0].get("url"),
            "versions": [r["tag"] for r in c.get("releases", [])],
            "installed": rec, "has_previous": (app_dir(name) / "previous").exists(),
            "update_available": bool(rec and lt and vkey(lt) > vkey(rec["version"])),
            "job": jobs.get(name), "path": f"app/{name}/",
            "latest_age_hours": age_hours((c.get("releases") or [{}])[0].get("published", "")) if lt else None,
            "packages_size": dir_size(PACKAGES / name)}


@app.get("/api/apps")
def list_apps():
    names = sorted(set(state["catalog"]) | set(state["installed"]))
    return {"apps": [view(n) for n in names], "checked_at": state["checked_at"], "settings": state["settings"],
            "problems": problems, "fresh_hours": FRESH_HOURS, "notify": bool(NOTIFY_URL)}


@app.post("/api/refresh")
async def refresh():
    ok = await refresh_catalog()
    if not ok:
        raise HTTPException(502, state["log"][0]["msg"] if state["log"] else "refresh failed")
    return list_apps()


def need(name):
    if name not in state["catalog"] and name not in state["installed"]:
        raise HTTPException(404, "unknown app")


@app.post("/api/apps/{name}/install")
async def install(name: str, body: dict = Body(default={})):
    need(name)
    if locks.get(name) and locks[name].locked():
        raise HTTPException(409, "busy")
    start(name, do_install(name, body.get("version"), "install" if name not in state["installed"] else "update"))
    return {"ok": True}


@app.post("/api/apps/{name}/rollback")
async def rollback(name: str):
    need(name)
    base = app_dir(name)
    rec = state["installed"].get(name)
    if not rec or not (base / "previous").exists():
        raise HTTPException(400, "nothing to roll back to")
    async with locks.setdefault(name, asyncio.Lock()):
        cur, prev, tmp = base / "current", base / "previous", base / "swap"
        cur.rename(tmp); prev.rename(cur); tmp.rename(prev)
        rec["version"], rec["previous"] = rec["previous"], rec["version"]
        rec["size"] = await asyncio.to_thread(dir_size, cur)
        log(f"Rolled back {name} to {rec['version']}")
    return {"ok": True}


@app.delete("/api/apps/{name}")
async def delete(name: str):
    async with locks.setdefault(name, asyncio.Lock()):
        if name not in state["installed"]:
            raise HTTPException(404, "not installed")
        shutil.rmtree(app_dir(name), ignore_errors=True)
        state["installed"].pop(name)
        log(f"Deleted {name}")
    return {"ok": True}


@app.post("/api/apps/{name}/settings")
def app_settings(name: str, body: dict = Body(...)):
    rec = state["installed"].get(name)
    if not rec:
        raise HTTPException(404, "not installed")
    if "auto_update" in body:
        rec["auto_update"] = bool(body["auto_update"])
    save()
    return {"ok": True}


@app.post("/api/settings")
def set_settings(body: dict = Body(...)):
    s = state["settings"]
    if "auto_update_all" in body:
        s["auto_update_all"] = bool(body["auto_update_all"])
    if "check_hours" in body:
        try:
            v = float(body["check_hours"])
        except (TypeError, ValueError):
            raise HTTPException(422, "check_hours must be a number")
        if not 0.25 <= v <= 720:
            raise HTTPException(422, "check_hours must be between 0.25 and 720")
        s["check_hours"] = v
    save()
    return s


@app.post("/api/upgrade-all")
async def upgrade_all():
    n = 0
    for name, rec in state["installed"].items():
        lt = latest_of(name)
        if lt and vkey(lt) > vkey(rec["version"]) and not (locks.get(name) and locks[name].locked()):
            start(name, do_install(name, lt, "update"))
            n += 1
    return {"started": n}


@app.post("/api/apps/{name}/dismiss")
def dismiss(name: str):
    if jobs.get(name, {}).get("state") == "error":
        jobs.pop(name)
    return {"ok": True}


@app.get("/api/export")
def export():
    return {"crafthub": 1, "apps": [{"name": n, "version": r["version"], "auto_update": r.get("auto_update", False)} for n, r in state["installed"].items()],
            "settings": state["settings"]}


@app.post("/api/import")
async def import_apps(body: dict = Body(...)):
    n = 0
    for a in body.get("apps", []):
        name = a.get("name")
        if name in state["catalog"] and name not in state["installed"] and not (locks.get(name) and locks[name].locked()):
            start(name, do_install(name, a.get("version"), "install"))
            if a.get("auto_update"):
                auto_flags[name] = True
            n += 1
    return {"started": n}


@app.get("/api/log")
def get_log():
    return state["log"]


@app.get("/api/health")
def health():
    return {"ok": True, "installed": len(state["installed"])}


# ---------- hosting the apps ----------
HASHED = re.compile(r"[-_.][0-9a-f]{8,}[-_.]|[-_.][0-9a-f]{8,}\.")


@app.api_route("/app/{name}", methods=["GET", "HEAD"])
def app_redirect(name: str):
    return RedirectResponse(f"/app/{name}/")


@app.api_route("/app/{name}/{path:path}", methods=["GET", "HEAD"])
def serve_app(name: str, request: Request, path: str = ""):
    root = (app_dir(name) / "current").resolve()
    if name not in state["installed"] or not root.exists():
        raise HTTPException(404, f"{name} is not installed")
    f = (root / (path or "index.html")).resolve()
    if root not in f.parents or not f.is_file():
        raise HTTPException(404)
    ctype = "application/wasm" if f.suffix == ".wasm" else ("text/javascript" if f.suffix == ".js" else mimetypes.guess_type(f.name)[0] or "application/octet-stream")
    headers = {"X-Content-Type-Options": "nosniff", "Vary": "Accept-Encoding",
               "Cache-Control": "public, max-age=31536000, immutable" if f.suffix in (".wasm", ".js") and HASHED.search(f.name) else "no-cache"}
    gz = Path(str(f) + ".gz")
    if gz.exists() and "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
        return FileResponse(gz, media_type=ctype, headers=headers)
    return FileResponse(f, media_type=ctype, headers=headers)


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return HTMLResponse((UI / "index.html").read_text(), headers={"Cache-Control": "no-cache"})


@app.get("/ui/{asset}")
def ui_asset(asset: str):
    f = UI / asset
    if asset not in ("app.js",) or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="text/javascript", headers={"Cache-Control": "no-cache"})
