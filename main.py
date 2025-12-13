# main.py
import os
import re
import time
import json
import tempfile
import shutil
import base64
import logging
import subprocess
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("unity-cloudbuilder")

app = FastAPI(title="Unity Cloud Build Trigger")

# --- ENV (set these on Render) ---
UNITY_API_BASE = os.getenv("UNITY_API_BASE", "https://build-api.cloud.unity3d.com/api/v1")
UNITY_ORG_ID = os.getenv("UNITY_CLOUD_BUILD_ORG_ID")
UNITY_PROJECT_ID = os.getenv("UNITY_CLOUD_BUILD_PROJECT_ID")
UNITY_API_KEY = os.getenv("UNITY_CLOUD_BUILD_API_KEY")
DEFAULT_BRANCH = os.getenv("DEFAULT_BRANCH", "main")
# a comma separated fallback list of Unity versions to try if we cannot detect, e.g. "2021.3.32f1,2021.3.39f1"
UNITY_VERSION_CANDIDATES = os.getenv("UNITY_VERSION_CANDIDATES", "2021.3.32f1,2021.3.39f1,2020.3.40f1").split(",")

if not (UNITY_ORG_ID and UNITY_PROJECT_ID and UNITY_API_KEY):
    logger.warning("Missing Unity env vars: set UNITY_CLOUD_BUILD_ORG_ID, UNITY_CLOUD_BUILD_PROJECT_ID, UNITY_CLOUD_BUILD_API_KEY")

# --- Models ---
class BuildRepoRequest(BaseModel):
    repo_url: HttpUrl
    branch: Optional[str] = DEFAULT_BRANCH
    project_name: Optional[str] = None
    wait_for_build: Optional[bool] = False  # set true to wait for builds to complete (may take minutes)

# --- Helpers ---
def unity_headers() -> Dict[str,str]:
    token = base64.b64encode(f":{UNITY_API_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

def run_cmd(cmd: List[str], cwd: Optional[str] = None):
    logger.info("run_cmd: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        logger.error("Command failed: %s", e.stderr.decode(errors="ignore"))
        raise RuntimeError(e.stderr.decode(errors="ignore"))

def clone_repo(repo_url: str, branch: str, tmpdir: str):
    # shallow clone single branch
    try:
        run_cmd(["git", "clone", "--depth", "1", "--branch", branch, repo_url, tmpdir])
    except RuntimeError as e:
        # fallback: try clone default branch without specifying branch
        logger.warning("Shallow clone failed for branch %s: %s. Trying default clone...", branch, str(e))
        try:
            run_cmd(["git", "clone", "--depth", "1", repo_url, tmpdir])
        except RuntimeError as e2:
            raise HTTPException(status_code=502, detail=f"Git clone failed: {e2}")

def detect_unity_version_from_repo(path: str) -> Optional[str]:
    candidate = os.path.join(path, "ProjectSettings", "ProjectVersion.txt")
    if not os.path.exists(candidate):
        logger.info("ProjectVersion.txt not found in repo")
        return None
    with open(candidate, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = re.search(r"m_EditorVersion:\s*(.+)", line)
            if m:
                ver = m.group(1).strip()
                logger.info("Detected unity version from repo: %s", ver)
                return ver
    return None

def list_build_targets() -> List[Dict[str,Any]]:
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets"
    r = requests.get(url, headers=unity_headers(), timeout=30)
    if r.status_code >= 400:
        logger.error("List targets failed: %s", r.text)
        raise HTTPException(status_code=502, detail=r.text)
    data = r.json()
    # normalise to list
    if isinstance(data, dict) and "items" in data:
        return data["items"]
    if isinstance(data, list):
        return data
    return [data]

def find_target(repo_url: str, branch: str, platform: str) -> Optional[Dict[str,Any]]:
    try:
        targets = list_build_targets()
    except HTTPException:
        return None
    for t in targets:
        # try common keys
        repo_val = t.get("repository") or t.get("source", {}).get("url") or t.get("scm", {}).get("url")
        t_branch = t.get("branch") or t.get("source", {}).get("branch") or t.get("scm", {}).get("branch")
        t_platform = t.get("buildTarget") or t.get("platform") or t.get("target")
        # loose matching
        if repo_val and (repo_val in repo_url or repo_url in repo_val):
            if (not branch) or (t_branch and t_branch == branch):
                if not platform or (t_platform and str(t_platform).lower() == str(platform).lower()):
                    return t
    return None

def create_build_target(repo_url: str, branch: str, name: str, platform: str, unity_version: str) -> Dict[str, Any]:
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets"
    payload = {
        "name": name,
        "buildTarget": platform,
        "repositoryType": "git",
        "repository": repo_url,
        "branch": branch,
        "source": {"type": "git", "url": repo_url, "branch": branch},
        "scm": {"type": "git", "url": repo_url, "branch": branch},
        "settings": {"unityVersion": unity_version}
    }
    r = requests.post(url, json=payload, headers=unity_headers(), timeout=60)
    if r.status_code >= 400:
        logger.error("Create target failed: %s", r.text)
        # return JSON body if parseable
        try:
            raise HTTPException(status_code=502, detail=r.json())
        except Exception:
            raise HTTPException(status_code=502, detail=r.text)
    return r.json()

def try_create_target_with_candidates(repo_url: str, branch: str, name: str, platform: str, candidates: List[str]) -> Dict[str,Any]:
    last_err = None
    for v in candidates:
        try:
            logger.info("Trying create target with unity version: %s", v)
            return create_build_target(repo_url, branch, name, platform, v)
        except HTTPException as e:
            last_err = e
            # if unity version not found, try next candidate
            detail = e.detail
            # detail may be dict or string; convert to string to inspect
            s = json.dumps(detail) if not isinstance(detail, str) else detail
            if "Unity version" in s or "not found" in s or "Unity version" in str(detail):
                logger.info("Unity version %s rejected by API, trying next candidate", v)
                continue
            else:
                # other error -> rethrow
                raise
    # if none succeeded
    if last_err:
        raise HTTPException(status_code=500, detail=f"No supported Unity versions found. Last error: {last_err.detail}")
    raise HTTPException(status_code=500, detail="Unknown error creating build target")

def trigger_build(target_id: str) -> Dict[str, Any]:
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets/{target_id}/builds"
    r = requests.post(url, headers=unity_headers(), json={}, timeout=60)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=r.text)
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}

def extract_build_number(resp: Dict[str,Any]) -> Optional[int]:
    # try common fields
    if not resp:
        return None
    if isinstance(resp, dict):
        if "build" in resp and isinstance(resp["build"], dict):
            for k in ("build","number","id"):
                if k in resp["build"]:
                    try: return int(resp["build"][k])
                    except: pass
        for k in ("build","buildNumber","build_number","number","id"):
            if k in resp:
                try: return int(resp[k])
                except: pass
    return None

def poll_build(target_id: str, build_number: int, timeout_seconds: int = 1800, poll_interval: int = 20) -> Dict[str,Any]:
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets/{target_id}/builds/{build_number}"
    start = time.time()
    while time.time() - start < timeout_seconds:
        r = requests.get(url, headers=unity_headers(), timeout=60)
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=r.text)
        info = r.json()
        build_info = info.get("build") if isinstance(info, dict) and "build" in info else info
        status = build_info.get("buildStatus") or build_info.get("status") or build_info.get("result")
        logger.info("Polling build %s/%s -> %s", target_id, build_number, status)
        if status and str(status).lower() in ("success","succeeded","finished"):
            return build_info
        if status and str(status).lower() in ("failure","failed","canceled","error"):
            raise HTTPException(status_code=500, detail={"message":"Build failed", "info": build_info})
        time.sleep(poll_interval)
    raise HTTPException(status_code=504, detail="Build polling timed out")

def create_share(target_id: str, build_number: int) -> Dict[str,Any]:
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets/{target_id}/builds/{build_number}/share"
    r = requests.post(url, headers=unity_headers(), json={}, timeout=60)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=r.text)
    try:
        return r.json()
    except:
        return {"raw": r.text}

# -----------------------
# Endpoint
# -----------------------
@app.post("/build_repo")
def build_repo(req: BuildRepoRequest):
    repo_url = str(req.repo_url)
    branch = (req.branch or DEFAULT_BRANCH).strip()
    project_hint = (req.project_name or "auto-game").strip()
    wait = bool(req.wait_for_build)

    if not repo_url:
        raise HTTPException(status_code=400, detail="repo_url required")
    if not branch:
        raise HTTPException(status_code=400, detail="branch required")

    tmp = tempfile.mkdtemp(prefix="repo_")
    try:
        clone_repo(repo_url, branch, tmp)
        unity_ver = detect_unity_version_from_repo(tmp)
        if not unity_ver:
            # fallback to candidates
            unity_ver = None
            logger.info("No unity version detected in repo, will try candidates: %s", UNITY_VERSION_CANDIDATES)
            candidates = UNITY_VERSION_CANDIDATES
        else:
            candidates = [unity_ver] + [v for v in UNITY_VERSION_CANDIDATES if v != unity_ver]

        # create or reuse webgl target
        webgl_target = find_target(repo_url, branch, "WebGL")
        if webgl_target:
            webgl_id = webgl_target.get("id") or webgl_target.get("buildTargetId") or webgl_target.get("targetId")
            logger.info("Reusing WebGL target id=%s", webgl_id)
        else:
            webgl_name = f"{project_hint}-WebGL"
            webgl_created = try_create_target_with_candidates(repo_url, branch, webgl_name, "WebGL", candidates)
            webgl_id = webgl_created.get("id") or webgl_created.get("buildTargetId") or webgl_created.get("targetId")

        # create or reuse android target
        android_target = find_target(repo_url, branch, "Android")
        if android_target:
            android_id = android_target.get("id") or android_target.get("buildTargetId") or android_target.get("targetId")
            logger.info("Reusing Android target id=%s", android_id)
        else:
            android_name = f"{project_hint}-Android"
            android_created = try_create_target_with_candidates(repo_url, branch, android_name, "Android", candidates)
            android_id = android_created.get("id") or android_created.get("buildTargetId") or android_created.get("targetId")

        result = {"repo": repo_url, "branch": branch, "webgl": {"target_id": webgl_id}, "android": {"target_id": android_id}}

        # Trigger builds
        wb_resp = trigger_build(webgl_id)
        ab_resp = trigger_build(android_id)
        wb_no = extract_build_number(wb_resp)
        ab_no = extract_build_number(ab_resp)
        result["webgl"]["build_no"] = wb_no
        result["android"]["build_no"] = ab_no

        if wait:
            if wb_no:
                webgl_final = poll_build(webgl_id, wb_no)
                share_w = create_share(webgl_id, wb_no)
                share_id = share_w.get("id") or share_w.get("shareid") or share_w.get("uuid")
                webgl_url = share_w.get("url") or (f"https://build.cloud.unity3d.com/share/{share_id}/webgl" if share_id else None)
                result["webgl"]["final"] = {"build_info": webgl_final, "share": share_w, "webgl_url": webgl_url}
            if ab_no:
                android_final = poll_build(android_id, ab_no)
                share_a = create_share(android_id, ab_no)
                share_id = share_a.get("id") or share_a.get("shareid") or share_a.get("uuid")
                apk_url = share_a.get("url") or (f"https://build.cloud.unity3d.com/share/{share_id}/" if share_id else None)
                result["android"]["final"] = {"build_info": android_final, "share": share_a, "apk_url": apk_url}

        return result

    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass

@app.get("/health")
def health():
    missing = [v for v in ("UNITY_CLOUD_BUILD_ORG_ID","UNITY_CLOUD_BUILD_PROJECT_ID","UNITY_CLOUD_BUILD_API_KEY") if not os.getenv(v)]
    return {"ok": len(missing)==0, "missing": missing}
