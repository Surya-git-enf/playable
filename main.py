# main.py
import os
import time
import base64
import zipfile
import requests
from io import BytesIO
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Prompt→Unity Auto Builder")

# ---------- Environment ----------
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # required
GITHUB_OWNER = os.getenv("GITHUB_OWNER")  # optional: org or user to create repos under; if empty uses authenticated user
UNITY_EMAIL = os.getenv("UNITY_EMAIL")    # required if trigger_build=True
UNITY_API_KEY = os.getenv("UNITY_API_KEY")# required if trigger_build=True
UNITY_ORG_ID = os.getenv("UNITY_ORG_ID")  # required if trigger_build=True
UNITY_PROJECT_ID = os.getenv("UNITY_PROJECT_ID")  # required if trigger_build=True
UNITY_TARGET_ID = os.getenv("UNITY_TARGET_ID")    # required if trigger_build=True (e.g., default-webgl)

# sanity
if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN environment variable is required")

GITHUB_API = "https://api.github.com"
UNITY_BUILD_API_BASE = "https://build-api.cloud.unity3d.com/api/v1"

# ---------- helpers ----------
def sanitize_name(s: str) -> str:
    import re
    s = s.strip().lower()
    s = re.sub(r'[^a-z0-9]', '', s)
    return s[:80] or "project"

def github_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

def get_authenticated_user():
    r = requests.get(f"{GITHUB_API}/user", headers=github_headers())
    r.raise_for_status()
    return r.json()["login"]

def repo_exists(owner: str, repo: str) -> bool:
    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=github_headers())
    return r.status_code == 200

def create_repo(owner: str, repo: str, private: bool = True):
    # if owner provided and different from authenticated user => org endpoint
    auth_user = get_authenticated_user()
    if owner and owner != auth_user:
        url = f"{GITHUB_API}/orgs/{owner}/repos"
        payload = {"name": repo, "private": private}
    else:
        url = f"{GITHUB_API}/user/repos"
        payload = {"name": repo, "private": private}
    r = requests.post(url, json=payload, headers=github_headers())
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Failed to create repo: {r.status_code} {r.text}")
    return r.json()

def upload_file_to_repo(owner: str, repo: str, path: str, content_bytes: bytes, message: str="Add file"):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    b64 = base64.b64encode(content_bytes).decode()
    payload = {"message": message, "content": b64, "branch": "main"}
    r = requests.put(url, json=payload, headers=github_headers())
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Failed to upload {path}: {r.status_code} {r.text}")
    return r.json()

def push_zip_to_repo(owner: str, repo: str, zip_path: str):
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=400, detail=f"Template zip not found at {zip_path}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        # ensure main branch exists: GitHub will create branch when first file is uploaded
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            with zf.open(member) as f:
                data = f.read()
                # GitHub paths should not start with leading slash
                path = member.lstrip("/")
                upload_file_to_repo(owner, repo, path, data, message=f"Add {path}")

def unity_basic_auth_header():
    token = base64.b64encode(f"{UNITY_EMAIL}:{UNITY_API_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

def trigger_unity_build(org_id, project_id, target_id, commit="main", timeout_sec=900):
    if not all([UNITY_EMAIL, UNITY_API_KEY, org_id, project_id, target_id]):
        return {"triggered": False, "reason": "Unity configuration incomplete"}

    build_url = f"{UNITY_BUILD_API_BASE}/orgs/{org_id}/projects/{project_id}/buildtargets/{target_id}/builds"
    payload = {"clean": True, "delay": 0, "commit": commit}
    r = requests.post(build_url, headers=unity_basic_auth_header(), json=payload)
    if r.status_code >= 400:
        return {"triggered": False, "status_code": r.status_code, "detail": r.text}

    # parse returned build object to get build number or poll list
    try:
        resp = r.json()
    except Exception:
        return {"triggered": True, "response": r.text}

    # get builds list endpoint for polling
    poll_url = f"{UNITY_BUILD_API_BASE}/orgs/{org_id}/projects/{project_id}/buildtargets/{target_id}/builds"
    # poll until success/failure
    start = time.time()
    while time.time() - start < timeout_sec:
        time.sleep(6)
        s = requests.get(poll_url, headers=unity_basic_auth_header())
        if s.status_code != 200:
            continue
        arr = s.json()
        if not isinstance(arr, list) or len(arr) == 0:
            continue
        latest = arr[0]  # most recent
        status = latest.get("buildStatus") or latest.get("status") or latest.get("state")
        if status and status.lower() in ("success", "built", "successfully_built"):
            # try to find download link
            links = latest.get("links") or {}
            dl = None
            # primary download might be in links.download_primary.href or links.downloads
            if "download_primary" in links:
                dl = links["download_primary"].get("href")
            elif "downloads" in links:
                dl = links["downloads"].get("primary", {}).get("href") if isinstance(links["downloads"], dict) else None
            # make absolute if relative
            if dl and dl.startswith("/"):
                dl = UNITY_BUILD_API_BASE + dl
            return {"triggered": True, "status": status, "build": latest, "download_url": dl}
        if status and status.lower() in ("failure", "failed", "cancelled"):
            return {"triggered": True, "status": status, "build": latest}
    return {"triggered": True, "status": "timeout", "detail": f"Build not finished in {timeout_sec}s"}

# ---------- API model ----------
class CreateGameRequest(BaseModel):
    username: str
    project_name: str
    prompt: Optional[str] = ""
    make_private: Optional[bool] = True
    trigger_build: Optional[bool] = True

# ---------- Endpoint ----------
@app.post("/create_game")
def create_game(req: CreateGameRequest):
    username = req.username.strip().lower()
    project_slug = sanitize_name(req.project_name)
    base_repo = f"{username}{project_slug}"
    owner = GITHUB_OWNER if GITHUB_OWNER else get_authenticated_user()

    # ensure unique name
    repo_name = base_repo
    i = 0
    while repo_exists(owner, repo_name):
        i += 1
        repo_name = f"{base_repo}{i}"

    # create repo
    repo_info = create_repo(owner, repo_name, private=req.make_private)
    repo_url = repo_info.get("html_url") or f"https://github.com/{owner}/{repo_name}"

    # push template zip (expect unity_template.zip present in working dir)
    template_zip = os.path.join(os.getcwd(), "unity_template.zip")
    push_zip_to_repo(owner, repo_name, template_zip)

    result = {"repo": repo_url, "repo_name": repo_name}

    if req.trigger_build:
        build_res = trigger_unity_build(UNITY_ORG_ID, UNITY_PROJECT_ID, UNITY_TARGET_ID, commit="main")
        result["build"] = build_res

    return result

# health
@app.get("/")
def root():
    return {"status": "ok"}
