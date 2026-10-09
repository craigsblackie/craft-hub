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
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

OWNER = os.getenv("GH_OWNER", "storytold")
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
EXCLUDE = {x.strip() for x in os.getenv("EXCLUDE_REPOS", "artcraft").split(",") if x.strip()}
EXTRA = [x.strip() for x in os.getenv("EXTRA_REPOS", "").split(",") if x.strip()]
REPO_RE = re.compile(os.getenv("REPO_PATTERN", r"^[a-z]+craft$"))
DATA = Path(os.getenv("DATA_DIR", "/data"))
APPS = DATA / "apps"
PACKAGES = Path(os.getenv("PACKAGES_DIR", "/packages"))
KEEP = int(os.getenv("KEEP_PACKAGES", "3"))
STATE_FILE = DATA / "state.json"
UI = Path(__file__).parent / "ui"
UA = {"User-Agent": "crafthub/1.0", "Accept": "application/vnd.github+json"}
if TOKEN:
    UA["Authorization"] = f"Bearer {TOKEN}"

REPLACES = {
    "photocraft": "Adobe Photoshop", "lightcraft": "Adobe Lightroom", "vectorcraft": "Adobe Illustrator",
    "filmcraft": "Adobe Premiere Pro", "pdfcraft": "Adobe Acrobat", "effectcraft": "Adobe After Effects",
    "designcraft": "Adobe InDesign", "soundcraft": "Avid Pro Tools", "wordcraft": "Microsoft Word",
    "gridcraft": "Microsoft Excel", "deckcraft": "Microsoft PowerPoint", "cadcraft": "AutoCAD",
}

state = {"catalog": {}, "installed": {}, "checked_at": 0, "settings": {"check_hours": 6.0, "auto_update_all": False}, "log": []}
jobs: dict = {}
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


# ---------- GitHub catalog ----------
async def gh(url, etag=None):
    h = {"If-None-Match": etag} if etag else {}
    r = await client.get(url, headers=h)
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise RuntimeError("GitHub API rate limit hit (set GITHUB_TOKEN to raise it)")
    return r


def parse_releases(rels):
    out = []
    for rel in rels:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        web = next((a for a in rel["assets"] if re.search(r"-web-.*\.zip$", a["name"])), None)
        if not web:
            continue
        sums = next((a for a in rel["assets"] if a["name"].upper().startswith("SHA256SUMS")), None)
        out.append({"tag": rel["tag_name"], "published": rel["published_at"], "url": rel["html_url"], "body": (rel.get("body") or "")[:1500],
                    "asset": web["name"], "asset_url": web["browser_download_url"], "size": web["size"],
                    "sums_url": sums["browser_download_url"] if sums else None})
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
        for n, repo in names.items():
            old = cat.get(n, {})
            rr = await gh(f"https://api.github.com/repos/{OWNER}/{n}/releases?per_page=15", old.get("etag"))
            if rr.status_code == 304:
                rels = old.get("releases", [])
            elif rr.status_code == 200:
                rels = parse_releases(rr.json())
            else:
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
    for m in z.infolist():
        t = (dest / m.filename).resolve()
        if dest not in t.parents and t != dest:
            raise RuntimeError(f"unsafe path in zip: {m.filename}")
    z.extractall(dest)


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
            tmp = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=DATA))
            try:
                pkgdir = PACKAGES / name
                zpath = pkgdir / rel["asset"]
                h = hashlib.sha256()
                if zpath.exists():
                    job["msg"], job["pct"] = f"Using stored package {rel['tag']}", 40
                    h.update(zpath.read_bytes())
                else:
                    pkgdir.mkdir(parents=True, exist_ok=True)
                    part = zpath.with_suffix(".part")
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
                                job["pct"] = int(got / total * 80) if total else 40
                    part.replace(zpath)
                verified = False
                if rel["sums_url"]:
                    job["msg"] = "Verifying checksum"
                    sr = await client.get(rel["sums_url"])
                    for line in sr.text.splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and parts[-1].lstrip("*") == rel["asset"]:
                            if parts[0].lower() != h.hexdigest():
                                zpath.unlink(missing_ok=True)
                                raise RuntimeError("SHA256 mismatch, package discarded")
                            verified = True
                job["msg"], job["pct"] = "Extracting", 85
                stage = tmp / "stage"
                with zipfile.ZipFile(zpath) as z:
                    safe_extract(z, stage)
                idx = next(stage.rglob("index.html"), None)
                if not idx:
                    raise RuntimeError("package has no index.html")
                root = idx.parent
                job["msg"], job["pct"] = "Optimising", 92
                await asyncio.to_thread(precompress, root)
                job["msg"], job["pct"] = "Activating", 97
                base = app_dir(name)
                base.mkdir(parents=True, exist_ok=True)
                cur, prev = base / "current", base / "previous"
                old = state["installed"].get(name)
                if prev.exists():
                    shutil.rmtree(prev)
                if cur.exists():
                    cur.rename(prev)
                shutil.move(str(root), str(cur))
                rec = {"version": rel["tag"], "installed_at": int(time.time()), "size": dir_size(cur), "verified": verified,
                       "auto_update": (old or {}).get("auto_update", False), "previous": (old or {}).get("version") if old else None}
                state["installed"][name] = rec
                for old_pkg in sorted(pkgdir.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)[KEEP:]:
                    old_pkg.unlink(missing_ok=True)
                log(f"{dict(install="Installed", update="Updated")[action]} {name} {rel['tag']}" + (" (checksum verified)" if verified else ""))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            job.update(state="done", pct=100, msg="Done")
        except Exception as e:
            job.update(state="error", msg=str(e) or type(e).__name__)
            log(f"{action} {name} failed: {job['msg']}", "error")


def cleanup_job_later(name):
    async def _c():
        await asyncio.sleep(8)
        j = jobs.get(name)
        if j and j["state"] != "running":
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


async def auto_update():
    for name, rec in list(state["installed"].items()):
        lt = latest_of(name)
        if (rec.get("auto_update") or state["settings"]["auto_update_all"]) and lt and vkey(lt) > vkey(rec["version"]):
            log(f"Auto-updating {name} {rec['version']} -> {lt}")
            await do_install(name, lt, "update")


async def background():
    await asyncio.sleep(2)
    while True:
        if time.time() - state["checked_at"] > state["settings"]["check_hours"] * 3600 or not state["catalog"]:
            if await refresh_catalog():
                await auto_update()
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
            "job": jobs.get(name), "path": f"app/{name}/"}


@app.get("/api/apps")
def list_apps():
    names = sorted(set(state["catalog"]) | set(state["installed"]))
    return {"apps": [view(n) for n in names], "checked_at": state["checked_at"], "settings": state["settings"]}


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
        rec["size"] = dir_size(cur)
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
        s["check_hours"] = max(0.25, float(body["check_hours"]))
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


@app.get("/api/log")
def get_log():
    return state["log"]


@app.get("/api/health")
def health():
    return {"ok": True, "installed": len(state["installed"])}


# ---------- hosting the apps ----------
HASHED = re.compile(r"[-_.][0-9a-f]{8,}[-_.]|[-_.][0-9a-f]{8,}\.")


@app.get("/app/{name}")
def app_redirect(name: str):
    return RedirectResponse(f"/app/{name}/")


@app.get("/app/{name}/{path:path}")
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


@app.get("/")
def index():
    return HTMLResponse((UI / "index.html").read_text(), headers={"Cache-Control": "no-cache"})
