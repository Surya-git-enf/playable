# main.py
import os
import time
import base64
import logging
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
import requests

# -----------------------
# Basic logging
# -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("unity-builder")

# -----------------------
# Config from environment
# -----------------------
UNITY_API_BASE = os.getenv("UNITY_API_BASE", "https://build-api.cloud.unity3d.com/api/v1")
UNITY_ORG_ID = os.getenv("UNITY_CLOUD_BUILD_ORG_ID")
UNITY_PROJECT_ID = os.getenv("UNITY_CLOUD_BUILD_PROJECT_ID")
UNITY_API_KEY = os.getenv("UNITY_CLOUD_BUILD_API_KEY")
DEFAULT_BRANCH = os.getenv("DEFAULT_BRANCH", "main")

if not (UNITY_ORG_ID and UNITY_PROJECT_ID and UNITY_API_KEY):
    logger.warning("Warning: Some Unity env vars are missing. "
                   "Set UNITY_CLOUD_BUILD_ORG_ID, UNITY_CLOUD_BUILD_PROJECT_ID, UNITY_CLOUD_BUILD_API_KEY in Render.")

# -----------------------
# FastAPI app
# -----------------------
app = FastAPI(title="Unity Cloud Build Proxy (robust)")

# -----------------------
# Request model
# -----------------------
class BuildRequest(BaseModel):
    repo_url: HttpUrl
    branch: Optional[str] = None
    project_name: Optional[str] = None
    wait_for_build: Optional[bool] = False  # set true to block until builds finish (may take many minutes)

# -----------------------
# Helpers: Unity auth header
# -----------------------
def unity_auth_headers() -> Dict[str, str]:
    """Return headers with Basic auth using UNITY_API_KEY as password."""
    if not UNITY_API_KEY:
        raise HTTPException(status_code=500, detail="UNITY_CLOUD_BUILD_API_KEY not configured.")
    token = base64.b64encode(f":{UNITY_API_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

# -----------------------
# HTTP wrappers with helpful errors
# -----------------------
def http_post(url: str, json_payload: Dict[str, Any], headers: Dict[str, str], timeout: int = 120) -> Dict[str, Any]:
    logger.info("POST %s  payload keys=%s", url, list(json_payload.keys()))
    try:
        r = requests.post(url, json=json_payload, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        logger.exception("HTTP POST failed")
        raise HTTPException(status_code=502, detail=f"Request failed: {e}")
    if r.status_code >= 400:
        logger.error("Unity API error %s: %s", r.status_code, r.text)
        # try parse JSON body, otherwise return raw text
        try:
            body = r.json()
        except Exception:
            body = r.text
        raise HTTPException(status_code=502, detail={"status_code": r.status_code, "body": body})
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}

def http_get(url: str, headers: Dict[str, str], timeout: int = 60) -> Dict[str, Any]:
    logger.info("GET %s", url)
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        logger.exception("HTTP GET failed")
        raise HTTPException(status_code=502, detail=f"Request failed: {e}")
    if r.status_code >= 400:
        logger.error("Unity API error %s: %s", r.status_code, r.text)
        try:
            body = r.json()
        except Exception:
            body = r.text
        raise HTTPException(status_code=502, detail={"status_code": r.status_code, "body": body})
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}

# -----------------------
# Unity API helpers
# -----------------------
def list_build_targets() -> List[Dict[str, Any]]:
    """Get existing build targets for the project. Returns list of targets (may be empty)."""
    if not (UNITY_ORG_ID and UNITY_PROJECT_ID):
        raise HTTPException(status_code=500, detail="UNITY org/project not configured.")
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets"
    headers = unity_auth_headers()
    resp = http_get(url, headers)
    # resp may be dict with "items" or list directly; normalize
    if isinstance(resp, dict) and "items" in resp:
        return resp["items"]
    if isinstance(resp, list):
        return resp
    # fallback: try to find keys that look like targets
    return [resp]

def find_existing_target(repo_url: str, branch: str, platform: str) -> Optional[Dict[str, Any]]:
    """Return an existing target that matches repo_url + branch + platform (best-effort)."""
    try:
        targets = list_build_targets()
    except HTTPException:
        # if listing fails, don't block creation - return None
        return None

    for t in targets:
        # try common fields
        t_repo = (t.get("repository") or t.get("repo") or t.get("source") or {})
        # t_repo may be string or dict
        repo_val = None
        if isinstance(t_repo, str):
            repo_val = t_repo
        elif isinstance(t_repo, dict):
            repo_val = t_repo.get("url") or t_repo.get("repository") or t_repo.get("repo")
        # compare URLs loosely (endswith) to avoid variations (ssh vs https)
        if repo_val and repo_url.endswith(repo_val) or (repo_val.endswith(repo_url) if isinstance(repo_val, str) else False):
            # check branch and platform if possible
            t_branch = t.get("branch") or (t.get("source") or {}).get("branch")
            t_platform = t.get("buildTarget") or t.get("platform")
            if (not branch or (t_branch and t_branch == branch)) and (not platform or (t_platform and str(t_platform).lower() == platform.lower())):
                return t
        # fallback: compare repo_url substrings
        try:
            if isinstance(repo_val, str) and repo_val in repo_url:
                return t
        except Exception:
            pass
    return None

def create_build_target(repo_url: str, branch: str, build_target_name: str, platform: str) -> Dict[str, Any]:
    """Create a new build target. Must include branch for GitHub oauth repos."""
    if not (UNITY_ORG_ID and UNITY_PROJECT_ID):
        raise HTTPException(status_code=500, detail="UNITY_CLOUD_BUILD_ORG_ID or UNITY_CLOUD_BUILD_PROJECT_ID not set")
    if not branch:
        raise HTTPException(status_code=400, detail="Branch is required to create a build target. Provide 'branch' in request or DEFAULT_BRANCH env var.")
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets"
    headers = unity_auth_headers()

    # payload: include multiple shapes to maximize compatibility with Unity API versions
    payload = {
        "name": build_target_name,
        "buildTarget": platform,
        "repositoryType": "git",
        "repository": repo_url,
        "branch": branch,
        # also include nested source key (some API versions expect it)
        "source": {
            "type": "git",
            "url": repo_url,
            "branch": branch
        },
        # include an oauth shorthand (some endpoints accept 'scm' or 'oauth')
        "scm": {
            "type": "oauth",
            "url": repo_url,
            "branch": branch
        }
    }

    return http_post(url, payload, headers)

def trigger_build_for_target(target_id: str) -> Dict[str, Any]:
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets/{target_id}/builds"
    headers = unity_auth_headers()
    return http_post(url, {}, headers)

def get_build_status(target_id: str, build_number: int) -> Dict[str, Any]:
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets/{target_id}/builds/{build_number}"
    headers = unity_auth_headers()
    return http_get(url, headers)

def create_share_link(target_id: str, build_number: int) -> Dict[str, Any]:
    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets/{target_id}/builds/{build_number}/share"
    headers = unity_auth_headers()
    return http_post(url, {}, headers)

def wait_for_build_success(target_id: str, build_number: int, timeout_seconds: int = 1800, poll_interval: int = 30) -> Dict[str, Any]:
    """Poll until build returns success or failure; returns final build info."""
    start = time.time()
    while True:
        if time.time() - start > timeout_seconds:
            raise HTTPException(status_code=504, detail=f"Timeout waiting for build after {timeout_seconds}s")
        status_json = get_build_status(target_id, build_number)
        # build info may be in several shapes; attempt to find a status
        build_info = status_json.get("build") if isinstance(status_json, dict) and "build" in status_json else status_json
        status = None
        if isinstance(build_info, dict):
            status = build_info.get("buildStatus") or build_info.get("status") or build_info.get("result")
        logger.info("Polled build %s/%s -> %s", target_id, build_number, status)
        if status and str(status).lower() in ("success", "succeeded", "finished"):
            return build_info
        if status and str(status).lower() in ("failure", "failed", "canceled", "error"):
            raise HTTPException(status_code=500, detail={"message": "Build failed", "status": status, "info": build_info})
        time.sleep(poll_interval)

# -----------------------
# Utility: try extract build number from various response shapes
# -----------------------
def extract_build_number(build_resp: Dict[str, Any]) -> Optional[int]:
    if not build_resp:
        return None
    # try multiple keys and nested shapes
    candidates = []
    if isinstance(build_resp, dict):
        # common shapes
        if "build" in build_resp and isinstance(build_resp["build"], dict):
            candidates.append(build_resp["build"].get("build"))
            candidates.append(build_resp["build"].get("number"))
            candidates.append(build_resp["build"].get("id"))
        for k in ("build", "buildNumber", "build_number", "number", "id"):
            if k in build_resp:
                candidates.append(build_resp.get(k))
    for c in candidates:
        try:
            if c is None:
                continue
            return int(c)
        except Exception:
            pass
    return None

# -----------------------
# API endpoint: /build
# -----------------------
@app.post("/build")
def build_endpoint(req: BuildRequest):
    # get branch (explicit request value takes precedence, otherwise default)
    branch = (req.branch or DEFAULT_BRANCH or "").strip()
    if not branch:
        raise HTTPException(status_code=400, detail="Branch is required (provide 'branch' in JSON or set DEFAULT_BRANCH env var).")

    repo_url = str(req.repo_url)
    project_name = (req.project_name or "auto-game").strip()
    wait = bool(req.wait_for_build)

    headers = unity_auth_headers()  # validate key presence early

    logger.info("Build request repo=%s branch=%s project=%s wait=%s", repo_url, branch, project_name, wait)

    # Reuse existing targets if possible — prevents duplicate targets and avoids missing branch error
    webgl_platform = "WebGL"
    android_platform = "Android"

    webgl_target = find_existing_target(repo_url, branch, webgl_platform)
    android_target = find_existing_target(repo_url, branch, android_platform)

    if webgl_target:
        webgl_target_id = webgl_target.get("id") or webgl_target.get("buildTargetId") or webgl_target.get("targetId")
        logger.info("Reusing existing WebGL target id=%s", webgl_target_id)
    else:
        logger.info("Creating new WebGL target")
        payload_name = f"{project_name}-WebGL"
        webgl_target = create_build_target(repo_url, branch, payload_name, webgl_platform)
        webgl_target_id = webgl_target.get("id") or webgl_target.get("buildTargetId") or webgl_target.get("targetId")
        logger.info("Created WebGL target: %s", webgl_target_id)

    if android_target:
        android_target_id = android_target.get("id") or android_target.get("buildTargetId") or android_target.get("targetId")
        logger.info("Reusing existing Android target id=%s", android_target_id)
    else:
        logger.info("Creating new Android target")
        payload_name = f"{project_name}-Android"
        android_target = create_build_target(repo_url, branch, payload_name, android_platform)
        android_target_id = android_target.get("id") or android_target.get("buildTargetId") or android_target.get("targetId")
        logger.info("Created Android target: %s", android_target_id)

    if not webgl_target_id or not android_target_id:
        raise HTTPException(status_code=500, detail={"webgl_target": webgl_target, "android_target": android_target})

    # Trigger builds
    try:
        webgl_build_resp = trigger_build_for_target(webgl_target_id)
        android_build_resp = trigger_build_for_target(android_target_id)
    except HTTPException as e:
        logger.exception("Failed to trigger builds")
        raise e

    webgl_build_number = extract_build_number(webgl_build_resp)
    android_build_number = extract_build_number(android_build_resp)

    result = {
        "status": "started",
        "repo": repo_url,
        "branch": branch,
        "webgl": {"target_id": webgl_target_id, "build_resp": webgl_build_resp, "build_number": webgl_build_number},
        "android": {"target_id": android_target_id, "build_resp": android_build_resp, "build_number": android_build_number}
    }

    # If waiting requested, poll until build(s) finish and create share links
    if wait:
        try:
            if webgl_build_number:
                webgl_final_info = wait_for_build_success(webgl_target_id, webgl_build_number)
                webgl_share = create_share_link(webgl_target_id, webgl_build_number)
                result["webgl"]["final"] = {"build_info": webgl_final_info, "share": webgl_share}
            if android_build_number:
                android_final_info = wait_for_build_success(android_target_id, android_build_number)
                android_share = create_share_link(android_target_id, android_build_number)
                result["android"]["final"] = {"build_info": android_final_info, "share": android_share}
            result["status"] = "finished"
        except HTTPException as e:
            logger.exception("Error while waiting for builds")
            raise e
        except Exception as e:
            logger.exception("Unexpected error while waiting")
            raise HTTPException(status_code=500, detail=str(e))

    return result

# -----------------------
# Health
# -----------------------
@app.get("/health")
def health():
    missing = [v for v in ("UNITY_CLOUD_BUILD_ORG_ID", "UNITY_CLOUD_BUILD_PROJECT_ID", "UNITY_CLOUD_BUILD_API_KEY") if not os.getenv(v)]
    return {"ok": len(missing) == 0, "missing_env": missing}
