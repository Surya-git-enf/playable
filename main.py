# main.py
import os
import shutil
import uuid
import zipfile
import asyncio
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx

# CONFIG (set these as Render environment variables)
UNITY_API_BASE = os.environ.get("UNITY_API_BASE", "https://build-api.cloud.unity3d.com/api/v1")
UNITY_ORG = os.environ.get("UNITY_ORG")           # e.g. org GUID
UNITY_PROJECT = os.environ.get("UNITY_PROJECT")   # e.g. project GUID
UNITY_BASIC_AUTH = os.environ.get("UNITY_BASIC_AUTH")  # "Basic base64(id:secret)" or "Bearer ..."
# Where to store artifacts temporarily on the Render service
WORKDIR = os.environ.get("WORKDIR", "/tmp/unity_builds")
ARTIFACTS_DIR = os.path.join(WORKDIR, "artifacts")
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

app = FastAPI(title="Unity Cloud Build Proxy for Render")

client = httpx.AsyncClient(timeout=120.0)

def unity_auth_headers():
    if UNITY_BASIC_AUTH:
        return {"Authorization": UNITY_BASIC_AUTH}
    raise RuntimeError("Set UNITY_BASIC_AUTH env var on Render")

# 1) Trigger a Unity Cloud Build for an existing build target.
@app.post("/trigger_build")
async def trigger_build(build_target_id: str, clean: Optional[bool] = True):
    """
    Request body (form/query):
      build_target_id: the Unity Cloud Build build target ID (GUID)
    Returns: build-request metadata (Unity response)
    """
    if not UNITY_ORG or not UNITY_PROJECT:
        raise HTTPException(status_code=500, detail="UNITY_ORG or UNITY_PROJECT not configured as env vars")

    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG}/projects/{UNITY_PROJECT}/buildtargets/{build_target_id}/builds"
    payload = {"clean": bool(clean)}
    headers = unity_auth_headers()
    headers["Content-Type"] = "application/json"
    resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail={"unity_status": resp.status_code, "body": resp.text})
    data = resp.json()
    return JSONResponse({"status": "started", "unity": data})

# 2) Webhook endpoint for Unity Cloud Build to POST when build finishes
@app.post("/unity_webhook")
async def unity_webhook(request: Request, background: BackgroundTasks):
    """
    Unity will POST build events to this endpoint (configure in Cloud Build).
    The payload contains build status and artifact URL(s).
    We will verify (optionally) and if success, download the build artifact in background.
    """
    payload = await request.json()
    # IMPORTANT: verify webhook signature / token if Unity provides it (not implemented here)
    build_status = payload.get("buildStatus") or payload.get("status")
    build_target = payload.get("buildTarget") or payload.get("buildTargetId")
    build_id = payload.get("buildId") or payload.get("buildNumber") or str(uuid.uuid4())

    # If build succeeded, download artifact in background
    if str(build_status).lower() in ("success", "finished", "succeeded"):
        artifact_url = None
        # Unity Cloud Build typically returns an "links" or "artifacts" list — adapt to actual shape:
        if "links" in payload and isinstance(payload["links"], list):
            for link in payload["links"]:
                if link.get("rel") == "download":
                    artifact_url = link.get("href")
                    break
        # fallback: payload['artifactUrl'] etc
        artifact_url = artifact_url or payload.get("artifactUrl") or payload.get("downloadUrl")
        if artifact_url:
            # spawn background download + deployment
            background.add_task(download_and_publish, artifact_url, build_target, build_id)
        return JSONResponse({"received": True, "will_download": bool(artifact_url)})
    else:
        # build failed or in progress — log or store status
        return JSONResponse({"received": True, "status": build_status})

async def download_and_publish(artifact_url: str, build_target: str, build_id: str):
    """
    Downloads the Unity Cloud Build artifact (zip), unpacks and publishes.
    For testing: we store under ARTIFACTS_DIR/<build_id>/ and expose static files.
    For production: upload to S3 / push to static hosting (recommended).
    """
    outdir = Path(ARTIFACTS_DIR) / f"{build_target}_{build_id}"
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # Download zip
    async with client.stream("GET", artifact_url, headers=unity_auth_headers()) as r:
        if r.status_code >= 400:
            # log error
            return
        temp_zip = outdir / "artifact.zip"
        with open(temp_zip, "wb") as f:
            async for chunk in r.aiter_bytes():
                f.write(chunk)

    # Unzip
    try:
        with zipfile.ZipFile(temp_zip, "r") as z:
            z.extractall(outdir)
    except Exception:
        # maybe artifact is already a folder — try nothing
        pass

    # At this point you have extracted WebGL build files (index.html, .mem, .wasm etc)
    # Next: publish to static host.
    # OPTION A (fast test): leave files under ARTIFACTS_DIR and serve via endpoint /static/<name>/index.html
    # OPTION B (recommended): upload files to S3 or push to a static site repo (not implemented here)
    return

# 3) List artifacts (simple)
@app.get("/artifacts")
def list_artifacts():
    items = []
    for p in Path(ARTIFACTS_DIR).iterdir():
        if p.is_dir():
            items.append(str(p.name))
    return {"artifacts": items}

# 4) Get play URL (convenience)
@app.get("/play/{artifact_name}")
def play(artifact_name: str):
    # Render static file URL — if using Render static site you would publish elsewhere
    file_index = Path(ARTIFACTS_DIR) / artifact_name / "index.html"
    if not file_index.exists():
        raise HTTPException(status_code=404, detail="artifact not found")
    # Serve via Render by returning a direct path that Render's static server is configured for,
    # or implement a StaticFiles mount (see below). For now return a path relative to service.
    return {"play_url": f"/static_artifacts/{artifact_name}/index.html"}

# OPTIONAL: mount static folder in app (only for small testing; Render may not persist long-term)
from fastapi.staticfiles import StaticFiles
app.mount("/static_artifacts", StaticFiles(directory=ARTIFACTS_DIR), name="static_artifacts")
