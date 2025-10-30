# main.py
import os, time, base64, zipfile, requests, io
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Prompt→Unity→Netlify Automation")

# ---------- ENV ----------
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")                # required
GITHUB_OWNER = os.getenv("GITHUB_OWNER")                # optional (org or user). If empty, uses authenticated user
GITHUB_TEMPLATE_REPO = os.getenv("GITHUB_TEMPLATE_REPO")# optional "owner/template"

UNITY_EMAIL = os.getenv("UNITY_EMAIL")                  # required for build trigger
UNITY_API_KEY = os.getenv("UNITY_API_KEY")              # required for build trigger
UNITY_ORG_ID = os.getenv("UNITY_ORG_ID")                # required
UNITY_PROJECT_ID = os.getenv("UNITY_PROJECT_ID")        # required
UNITY_TARGET_ID = os.getenv("UNITY_TARGET_ID")          # required (default-webgl etc.)

NETLIFY_TOKEN = os.getenv("NETLIFY_TOKEN")              # required
NETLIFY_ACCOUNT_ID = os.getenv("NETLIFY_ACCOUNT_ID")    # optional (for site creation under account)

if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN required")

GITHUB_API = "https://api.github.com"
UNITY_BUILD_API_BASE = "https://build-api.cloud.unity3d.com/api/v1"
NETLIFY_API_BASE = "https://api.netlify.com/api/v1"

# ---------- helpers ----------
def sanitize(s: str) -> str:
    import re
    s = s.strip().lower()
    s = re.sub(r'[^a-z0-9]', '', s)
    return s[:80] or "project"

def github_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

def get_authenticated_user():
    r = requests.get(f"{GITHUB_API}/user", headers=github_headers())
    r.raise_for_status()
    return r.json()["login"]

def repo_exists(owner: str, repo: str) -> bool:
    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=github_headers())
    return r.status_code == 200

def create_repo(owner: str, repo: str, private=True):
    auth_user = get_authenticated_user()
    if owner and owner != auth_user:
        url = f"{GITHUB_API}/orgs/{owner}/repos"
        payload = {"name": repo, "private": private}
    else:
        url = f"{GITHUB_API}/user/repos"
        payload = {"name": repo, "private": private}
    r = requests.post(url, json=payload, headers=github_headers())
    if r.status_code not in (200,201):
        raise HTTPException(status_code=500, detail=f"Create repo failed: {r.status_code} {r.text}")
    return r.json()

def create_repo_from_template(template_owner, template_repo, owner, new_name, private=True):
    url = f"{GITHUB_API}/repos/{template_owner}/{template_repo}/generate"
    payload = {"owner": owner, "name": new_name, "private": private, "include_all_branches": False}
    r = requests.post(url, json=payload, headers=github_headers())
    if r.status_code not in (200,201):
        raise HTTPException(status_code=500, detail=f"Create from template failed: {r.status_code} {r.text}")
    return r.json()

def upload_file(owner, repo, path, content_bytes, message="Add file"):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    b64 = base64.b64encode(content_bytes).decode()
    payload = {"message": message, "content": b64, "branch": "main"}
    r = requests.put(url, json=payload, headers=github_headers())
    if r.status_code not in (200,201):
        raise HTTPException(status_code=500, detail=f"Upload {path} failed: {r.status_code} {r.text}")
    return r.json()

def push_zip_to_repo(owner, repo, zip_path):
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=400, detail="unity_template.zip missing in repo root")
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"): continue
            with zf.open(member) as f:
                data = f.read()
                path = member.lstrip("/")
                upload_file(owner, repo, path, data, message=f"Add {path}")

def unity_auth_header():
    token = base64.b64encode(f"{UNITY_EMAIL}:{UNITY_API_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

def trigger_unity_build(org_id, project_id, target_id, commit="main"):
    if not all([UNITY_EMAIL, UNITY_API_KEY, org_id, project_id, target_id]):
        return {"triggered": False, "reason": "Unity configuration incomplete"}
    url = f"{UNITY_BUILD_API_BASE}/orgs/{org_id}/projects/{project_id}/buildtargets/{target_id}/builds"
    payload = {"clean": True, "delay": 0, "commit": commit}
    r = requests.post(url, headers=unity_auth_header(), json=payload)
    if r.status_code >= 400:
        return {"triggered": False, "status_code": r.status_code, "detail": r.text}
    return {"triggered": True, "response": r.json()}

def poll_unity_build_for_artifact(org_id, project_id, target_id, timeout=900, poll_interval=6):
    poll_url = f"{UNITY_BUILD_API_BASE}/orgs/{org_id}/projects/{project_id}/buildtargets/{target_id}/builds"
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(poll_interval)
        r = requests.get(poll_url, headers=unity_auth_header())
        if r.status_code != 200: 
            continue
        arr = r.json()
        if not isinstance(arr, list) or len(arr)==0:
            continue
        latest = arr[0]
        status = (latest.get("buildStatus") or latest.get("status") or latest.get("state") or "").lower()
        if status in ("success","built","successfully_built"):
            links = latest.get("links",{})
            dl = None
            if "download_primary" in links:
                dl = links["download_primary"].get("href")
            elif "downloads" in links and isinstance(links["downloads"], dict):
                dl = links["downloads"].get("primary",{}).get("href")
            if dl and dl.startswith("/"):
                dl = UNITY_BUILD_API_BASE + dl
            return {"status": "success", "build": latest, "download_url": dl}
        if status in ("failure","failed","cancelled"):
            return {"status": "failed", "build": latest}
    return {"status": "timeout"}

def download_bytes(url):
    r = requests.get(url, stream=True)
    r.raise_for_status()
    return r.content

def netlify_headers():
    return {"Authorization": f"Bearer {NETLIFY_TOKEN}"}

def create_netlify_site(site_name=None):
    url = f"{NETLIFY_API_BASE}/sites"
    payload = {"name": site_name} if site_name else {}
    if NETLIFY_ACCOUNT_ID:
        payload["account"] = {"id": NETLIFY_ACCOUNT_ID}
    r = requests.post(url, json=payload, headers=netlify_headers())
    if r.status_code not in (200,201):
        raise HTTPException(status_code=500, detail=f"Create Netlify site failed: {r.status_code} {r.text}")
    return r.json()

def deploy_zip_to_netlify(site_id, zip_bytes):
    # POST zip as body to /sites/{site_id}/deploys with content-type application/zip
    url = f"{NETLIFY_API_BASE}/sites/{site_id}/deploys"
    headers = netlify_headers()
    headers["Content-Type"] = "application/zip"
    r = requests.post(url, data=zip_bytes, headers=headers)
    if r.status_code not in (200,201):
        raise HTTPException(status_code=500, detail=f"Netlify deploy failed: {r.status_code} {r.text}")
    return r.json()

# ---------- API model ----------
class CreateGameRequest(BaseModel):
    username: str
    project_name: str
    prompt: Optional[str] = ""
    make_private: Optional[bool] = True
    trigger_build: Optional[bool] = True

@app.post("/create_game")
def create_game(req: CreateGameRequest):
    username = req.username.strip().lower()
    slug = sanitize(req.project_name)
    base = f"{username}{slug}"
    owner = GITHUB_OWNER if GITHUB_OWNER else get_authenticated_user()

    # generate unique repo name
    repo_name = base
    i = 0
    while repo_exists(owner, repo_name):
        i += 1
        repo_name = f"{base}{i}"

    # create repo (template preferred)
    if GITHUB_TEMPLATE_REPO:
        t_owner, t_repo = GITHUB_TEMPLATE_REPO.split("/")
        repo_info = create_repo_from_template(t_owner, t_repo, owner, repo_name, private=req.make_private)
    else:
        repo_info = create_repo(owner, repo_name, private=req.make_private)
        # push unity_template.zip contents
        zip_path = os.path.join(os.getcwd(), "unity_template.zip")
        push_zip_to_repo(owner, repo_name, zip_path)

    repo_url = repo_info.get("html_url") or f"https://github.com/{owner}/{repo_name}"

    result = {"repo_owner": owner, "repo_name": repo_name, "repo_url": repo_url}

    if req.trigger_build:
        # trigger unity build
        trigger_res = trigger_unity_build(UNITY_ORG_ID, UNITY_PROJECT_ID, UNITY_TARGET_ID, commit="main")
        result["unity_trigger"] = trigger_res
        if not trigger_res.get("triggered"):
            return result

        # poll build for artifact
        poll = poll_unity_build_for_artifact(UNITY_ORG_ID, UNITY_PROJECT_ID, UNITY_TARGET_ID, timeout=900)
        result["unity_poll"] = poll
        if poll.get("status") != "success":
            return result

        dl = poll.get("download_url")
        if not dl:
            return result

        # download artifact zip bytes
        zip_bytes = download_bytes(dl)

        # create netlify site and deploy
        site_name = f"{username}{slug}"
        net_site = create_netlify_site(site_name)
        site_id = net_site.get("id")
        deploy_info = deploy_zip_to_netlify(site_id, zip_bytes)
        result["netlify_site"] = {"url": net_site.get("ssl_url") or net_site.get("url"), "site_id": site_id}
        result["netlify_deploy"] = deploy_info

    return result

@app.get("/")
def root():
    return {"status": "ok"}
