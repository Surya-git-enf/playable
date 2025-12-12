from fastapi import FastAPI, HTTPException
import os, time, requests
import base64
app = FastAPI()
base_url = "https://build-api.cloud.unity3d.com/api/v1"
org = os.environ["UNITY_CLOUD_BUILD_ORG_ID"]
proj = os.environ["UNITY_CLOUD_BUILD_PROJECT_ID"]
api_key = os.environ["UNITY_CLOUD_BUILD_API_KEY"]
auth_header = {
    "Authorization": "Basic " + base64.b64encode((api_key+":").encode()).decode()
}

@app.post("/build")
def trigger_build(data: dict):
    repo_url = data.get("repo_url")
    branch = data.get("branch", "main")
    if not repo_url:
        raise HTTPException(status_code=400, detail="Missing repo_url")
    # 1) Create WebGL build target
    payload_w = {
        "name": "Build-WebGL",
        "buildTarget": "WebGL",
        "repositoryType": "git",
        "repository": repo_url,
        "branch": branch,
        # ...other config...
    }
    r = requests.post(f"{base_url}/orgs/{org}/projects/{proj}/buildtargets", 
                      json=payload_w, headers=auth_header)
    r.raise_for_status()
    target_w = r.json()["id"]
    # 2) Create Android build target
    payload_a = { **payload_w, "name": "Build-Android", "buildTarget": "Android" }
    r = requests.post(f"{base_url}/orgs/{org}/projects/{proj}/buildtargets", 
                      json=payload_a, headers=auth_header)
    r.raise_for_status()
    target_a = r.json()["id"]
    # 3) Trigger builds
    build_w = requests.post(f"{base_url}/orgs/{org}/projects/{proj}/buildtargets/{target_w}/builds",
                            headers=auth_header)
    build_w.raise_for_status()
    build_no_w = build_w.json()["build"]["build"]
    build_a = requests.post(f"{base_url}/orgs/{org}/projects/{proj}/buildtargets/{target_a}/builds",
                            headers=auth_header)
    build_a.raise_for_status()
    build_no_a = build_a.json()["build"]["build"]
    # 4) Poll for completion (simple loop)
    def wait_for(target_id, build_no):
        status = ""
        while status not in ("success","failure"):
            time.sleep(30)
            res = requests.get(f"{base_url}/orgs/{org}/projects/{proj}/buildtargets/{target_id}/builds/{build_no}",
                               headers=auth_header)
            res.raise_for_status()
            status = res.json().get("buildStatus","")
        if status != "success":
            raise HTTPException(status_code=500, detail=f"Build failed ({status})")
    wait_for(target_w, build_no_w)
    wait_for(target_a, build_no_a)
    # 5) Create shareable links
    share_w = requests.post(f"{base_url}/orgs/{org}/projects/{proj}/buildtargets/{target_w}/builds/{build_no_w}/share",
                            headers=auth_header).json()
    share_a = requests.post(f"{base_url}/orgs/{org}/projects/{proj}/buildtargets/{target_a}/builds/{build_no_a}/share",
                            headers=auth_header).json()
    url_w = share_w.get("url")
    url_a = share_a.get("url")
    # 6) Return URLs
    return {"webgl_url": url_w, "apk_url": url_a}
