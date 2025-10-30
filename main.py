from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests, os, json

app = FastAPI()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
UNITY_API_KEY = os.getenv("UNITY_API_KEY")

class GameRequest(BaseModel):
    username: str
    project_name: str
    files: list  # list of {path, content}

@app.post("/build_game")
def build_game(req: GameRequest):
    repo_name = f"{req.username}{req.project_name}"
    
    # 1️⃣ Create or update repo
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    r = requests.get(f"https://api.github.com/repos/{req.username}/{repo_name}", headers=headers)
    
    if r.status_code == 404:
        requests.post("https://api.github.com/user/repos", headers=headers, json={"name": repo_name, "private": False})
    
    # 2️⃣ Push each file
    for f in req.files:
        file_url = f"https://api.github.com/repos/{req.username}/{repo_name}/contents/{f['path']}"
        content_encoded = f["content"].encode("utf-8")
        requests.put(file_url, headers=headers, json={
            "message": "update from AI",
            "content": content_encoded.decode("utf-8")  # simplified
        })
    
    # 3️⃣ Trigger Unity Cloud Build
    build_url = f"https://build-api.cloud.unity3d.com/api/v1/orgs/{os.getenv('UNITY_ORG_ID')}/projects/{os.getenv('UNITY_PROJECT_ID')}/buildtargets/default-webgl/builds"
    build_headers = {"Authorization": f"Basic {UNITY_API_KEY}"}
    response = requests.post(build_url, headers=build_headers)
    
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail=response.text)
    
    return {"message": "Build triggered successfully!", "build_info": response.json()}
