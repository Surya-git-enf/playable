import os, time
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()
# Load environment variables (set these in Render)
UNITY_ORG_ID = os.getenv("UNITY_CLOUD_BUILD_ORG_ID")
UNITY_PROJECT_ID = os.getenv("UNITY_CLOUD_BUILD_PROJECT_ID")
UNITY_API_KEY = os.getenv("UNITY_CLOUD_BUILD_API_KEY")
API_BASE = "https://build-api.cloud.unity3d.com/api/v1"

if not all([UNITY_ORG_ID, UNITY_PROJECT_ID, UNITY_API_KEY]):
    raise RuntimeError("Environment variables UNITY_CLOUD_BUILD_ORG_ID, "
                       "UNITY_CLOUD_BUILD_PROJECT_ID, UNITY_CLOUD_BUILD_API_KEY must be set.")

class BuildRequest(BaseModel):
    repo_url: str
    branch: str = "main"

def unity_api_request(method, path, json=None):
    """Helper to make a Unity Cloud Build API call with Basic Auth."""
    url = f"{API_BASE}{path}"
    auth = (UNITY_API_KEY, "")  # Basic auth with API key
    headers = {"Content-Type": "application/json"}
    resp = requests.request(method, url, auth=auth, json=json, headers=headers)
    if not resp.ok:
        raise HTTPException(status_code=resp.status_code,
                            detail=f"Unity API error: {resp.text}")
    return resp.json()

@app.post("/build")
def trigger_build(req: BuildRequest):
    branch = req.branch or "main"
    org = UNITY_ORG_ID
    project = UNITY_PROJECT_ID
    repo_url = req.repo_url

    # 1. Update project repo URL (ensure the Unity project is linked to this Git URL)
    try:
        unity_api_request("PUT", f"/orgs/{org}/projects/{project}", json={
            "settings": { "scm": { "url": repo_url, "type": "git", "branch": branch } }
        })
    except HTTPException as e:
        # If project not found or update fails, proceed only if builds can run (project may already be configured)
        if e.status_code != 404:
            raise

    # 2. Create build targets
    targets = []
    for platform, bundle_id in [("WebGL", ""), ("Android", "com.example.mygame")]:
        target_name = f"{platform}-Build"
        settings = {
            "platform": {"bundleId": bundle_id} if bundle_id else {},
            "autoBuild": True,
            "scm": {"type": "git", "branch": branch},
            "unityVersion": "latest"
        }
        payload = {
            "platform": platform.lower(),
            "name": target_name,
            "enabled": True,
            "settings": settings
        }
        # Create the build target
        tgt = unity_api_request("POST", f"/orgs/{org}/projects/{project}/buildtargets", json=payload)
        target_id = tgt.get("buildtargetid") or tgt.get("id") or tgt.get("buildTargetId")
        if not target_id:
            raise HTTPException(status_code=500, detail=f"Failed to create {platform} target")
        targets.append((platform, target_id))

    # 3. Trigger builds and poll status
    share_links = {}
    for platform, tid in targets:
        # Start build
        build_resp = unity_api_request("POST",
            f"/orgs/{org}/projects/{project}/buildtargets/{tid}/builds",
            json={"clean": False, "delay": 0}
        )
        # Assume build_resp is a list with a 'build' number
        build_number = None
        if isinstance(build_resp, list) and build_resp:
            build_number = build_resp[0].get("build")
        elif isinstance(build_resp, dict):
            build_number = build_resp.get("build")
        if not build_number:
            raise HTTPException(status_code=500, detail=f"Failed to start {platform} build")

        # Poll until done
        status = None
        for _ in range(20):  # max polls ~20 times
            time.sleep(5)
            info = unity_api_request("GET",
                f"/orgs/{org}/projects/{project}/buildtargets/{tid}/builds/{build_number}")
            status = info.get("buildStatus") or info.get("status")
            if status in ("success", "failed"):
                break
        if status != "success":
            raise HTTPException(status_code=500, detail=f"{platform} build failed (status={status})")

        # Create share link
        share_resp = unity_api_request("POST",
            f"/orgs/{org}/projects/{project}/buildtargets/{tid}/builds/{build_number}/share")
        share_id = share_resp.get("shareid") or share_resp.get("id")
        if not share_id:
            raise HTTPException(status_code=500, detail=f"Failed to create share for {platform}")
        # Construct share URL (WebGL pages vs direct APK download)
        if platform == "WebGL":
            url = f"https://build.cloud.unity3d.com/share/{share_id}/webgl"
        else:
            url = f"https://build.cloud.unity3d.com/share/{share_id}/"
        share_links[platform] = url

    return {"status": "builds_triggered", "links": share_links}
