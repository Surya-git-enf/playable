# main.py
import os
import time
import base64
import logging
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, HttpUrl
import requests

# --- Basic logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("unity-builder")

# --- Config from env ---
UNITY_API_BASE = os.getenv("UNITY_API_BASE", "https://build-api.cloud.unity3d.com/api/v1")
UNITY_ORG_ID = os.getenv("UNITY_CLOUD_BUILD_ORG_ID")
UNITY_PROJECT_ID = os.getenv("UNITY_CLOUD_BUILD_PROJECT_ID")
UNITY_API_KEY = os.getenv("UNITY_CLOUD_BUILD_API_KEY")
# Optional default branch if not provided in request
DEFAULT_BRANCH = os.getenv("DEFAULT_BRANCH", "main")

if not (UNITY_ORG_ID and UNITY_PROJECT_ID and UNITY_API_KEY):
    logger.warning("One or more Unity env vars are missing. "
                   "Ensure UNITY_CLOUD_BUILD_ORG_ID, UNITY_CLOUD_BUILD_PROJECT_ID, and UNITY_CLOUD_BUILD_API_KEY are set.")

# --- FastAPI app ---
app = FastAPI(title="Unity Cloud Build Proxy")

# --- Models ---
class BuildRequest(BaseModel):
    repo_url: HttpUrl
    branch: Optional[str] = DEFAULT_BRANCH
    project_name: Optional[str] = None
    wait_for_build: Optional[bool] = False  # if true, endpoint waits until builds finish (may be long)


# --- Helpers: Unity auth header (Basic with api_key as password) ---
def unity_auth_headers() -> Dict[str, str]:
    """
    Unity Cloud Build uses Basic auth with API key as the password.
    Username can be blank. We encode ":{api_key}" as base64.
    """
    if not UNITY_API_KEY:
        raise RuntimeError("UNITY_CLOUD_BUILD_API_KEY is not configured in environment variables.")
    token = base64.b64encode((f":{UNITY_API_KEY}").encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


# --- Utility: HTTP request wrapper with error handling ---
def http_post(url: str, json: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    logger.info("POST %s", url)
    try:
        r = requests.post(url, json=json, headers=headers, timeout=120)
    except requests.RequestException as e:
        logger.error("Request failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Request to Unity failed: {e}")
    if r.status_code >= 400:
        logger.error("Unity API returned %s: %s", r.status_code, r.text)
        raise HTTPException(status_code=502, detail={"status_code": r.status_code, "body": r.text})
    try:
        return r.json()
    except Exception:
        return {"raw_text": r.text}


def http_get(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    logger.info("GET %s", url)
    try:
        r = requests.get(url, headers=headers, timeout=60)
    except requests.RequestException as e:
        logger.error("Request failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Request to Unity failed: {e}")
    if r.status_code >= 400:
        logger.error("Unity API returned %s: %s", r.status_code, r.text)
        raise HTTPException(status_code=502, detail={"status_code": r.status_code, "body": r.text})
    try:
        return r.json()
    except Exception:
        return {"raw_text": r.text}


# --- Core steps: create target, trigger build, wait, share link ---
# --- inside main.py ---

# ensure DEFAULT_BRANCH is set earlier, for example: DEFAULT_BRANCH = os.getenv("DEFAULT_BRANCH", "main")

def create_build_target(repo_url: str, branch: str, build_target_name: str, platform: str) -> Dict[str, Any]:
    """
    Create a build target in Unity Cloud Build.
    Must include 'branch' for OAuth/GitHub repos (Unity requires branch).
    """
    if not (UNITY_ORG_ID and UNITY_PROJECT_ID):
        raise HTTPException(status_code=500, detail="UNITY_CLOUD_BUILD_ORG_ID or UNITY_CLOUD_BUILD_PROJECT_ID not set")

    # ensure branch is provided
    if not branch:
        raise HTTPException(status_code=400, detail="Branch is required to create a build target. Provide branch in POST /build as 'branch' (e.g. 'main').")

    url = f"{UNITY_API_BASE}/orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets"

    # Include branch explicitly (Unity expects it for oauth/scm)
    payload = {
        "name": build_target_name,
        "buildTarget": platform,
        "repositoryType": "git",
        "repository": repo_url,
        "branch": branch
    }

    headers = unity_auth_headers()
    # call wrapped http_post so errors are converted to HTTPException with Unity body
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
    """
    Poll build status until success/failure or timeout.
    Returns the final status JSON.
    """
    start = time.time()
    last_status = None
    while True:
        if time.time() - start > timeout_seconds:
            raise HTTPException(status_code=504, detail=f"Waiting for build timed out after {timeout_seconds}s")
        status_json = get_build_status(target_id, build_number)
        # find status field - API variants exist; try several names
        build_info = status_json.get("build", status_json)  # sometimes nested under "build"
        status = build_info.get("buildStatus") or build_info.get("status") or build_info.get("result") or None
        logger.info("Polled build %s/%s -> status: %s", target_id, build_number, status)
        last_status = build_info
        if status and str(status).lower() in ("success", "succeeded", "finished"):
            return build_info
        if status and str(status).lower() in ("failure", "failed", "canceled", "error"):
            raise HTTPException(status_code=500, detail={"message": "Build failed", "status": status, "details": build_info})
        time.sleep(poll_interval)


# --- API endpoints ---


@app.post("/build")
def build_endpoint(req: BuildRequest):
    """
    Accepts: { "repo_url": "...", "branch": "main", "project_name":"opt", "wait_for_build": false }
    Steps:
      1) create WebGL target
      2) create Android target
      3) trigger builds
      4) optionally wait for completion and return share links
    """
    repo_url = str(req.repo_url)
    branch = req.branch or DEFAULT_BRANCH
    project_name = req.project_name or "auto-game"
    wait = bool(req.wait_for_build)

    headers = unity_auth_headers()  # validate env existence

    logger.info("Starting build flow for repo=%s branch=%s", repo_url, branch)

    # Create targets
    try:
        webgl_name = f"{project_name}-WebGL"
        android_name = f"{project_name}-Android"
        webgl_target = create_build_target(repo_url, branch, webgl_name, "WebGL")
        android_target = create_build_target(repo_url, branch, android_name, "Android")
    except HTTPException as e:
        logger.exception("Failed creating build target")
        raise e
    except Exception as e:
        logger.exception("Unexpected error creating target")
        raise HTTPException(status_code=500, detail=str(e))

    webgl_target_id = webgl_target.get("id") or webgl_target.get("buildTargetId") or webgl_target.get("targetId")
    android_target_id = android_target.get("id") or android_target.get("buildTargetId") or android_target.get("targetId")
    if not webgl_target_id or not android_target_id:
        logger.error("Could not determine created target ids: webgl=%s android=%s", webgl_target, android_target)
        raise HTTPException(status_code=500, detail={"webgl_target": webgl_target, "android_target": android_target})

    # Trigger builds
    try:
        webgl_build_resp = trigger_build_for_target(webgl_target_id)
        android_build_resp = trigger_build_for_target(android_target_id)
    except HTTPException as e:
        logger.exception("Failed to trigger builds")
        raise e

    # Extract build numbers (best-effort)
    def extract_build_number(build_resp: Dict[str, Any]) -> Optional[int]:
        # try common shapes
        b = build_resp.get("build") or build_resp
        for key in ("build", "buildNumber", "build_number", "id"):
            if key in b:
                try:
                    return int(b[key])
                except Exception:
                    pass
        # sometimes response has "build": {"number":..}
        if isinstance(b, dict) and "number" in b:
            try:
                return int(b["number"])
            except Exception:
                pass
        return None

    webgl_build_number = extract_build_number(webgl_build_resp)
    android_build_number = extract_build_number(android_build_resp)

    result = {
        "status": "started",
        "repo": repo_url,
        "branch": branch,
        "webgl": {"target_id": webgl_target_id, "build_resp": webgl_build_resp, "build_number": webgl_build_number},
        "android": {"target_id": android_target_id, "build_resp": android_build_resp, "build_number": android_build_number}
    }

    # If client asked to wait, block until success (may take many minutes)
    if wait:
        try:
            if webgl_build_number:
                webgl_info = wait_for_build_success(webgl_target_id, webgl_build_number)
                # create share link
                share_w = create_share_link(webgl_target_id, webgl_build_number)
                result["webgl"]["final"] = {"build_info": webgl_info, "share": share_w}
            if android_build_number:
                android_info = wait_for_build_success(android_target_id, android_build_number)
                share_a = create_share_link(android_target_id, android_build_number)
                result["android"]["final"] = {"build_info": android_info, "share": share_a}
            result["status"] = "finished"
        except HTTPException as e:
            logger.exception("Build error while waiting")
            raise e
        except Exception as e:
            logger.exception("Unexpected error while waiting")
            raise HTTPException(status_code=500, detail=str(e))

    return result


@app.get("/health")
def health():
    ok = True
    missing = []
    for var in ("UNITY_CLOUD_BUILD_ORG_ID", "UNITY_CLOUD_BUILD_PROJECT_ID", "UNITY_CLOUD_BUILD_API_KEY"):
        if not os.getenv(var):
            missing.append(var)
            ok = False
    return {"ok": ok, "missing_env": missing}
