# main.py
import os
import uuid
import json
from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from datetime import datetime

# Paths (must match worker)
JOBS_DIR = os.getenv("JOBS_DIR", "/app/jobs")
BUILD_DIR = os.getenv("BUILD_DIR", "/app/builds")
os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(BUILD_DIR, exist_ok=True)

app = FastAPI()

# Serve built sites at /static/<job_id>/index.html
app.mount("/static", StaticFiles(directory=BUILD_DIR), name="static")

class BuildRequest(BaseModel):
    repo_url: str

@app.post("/build")
def start_build(req: BuildRequest):
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    job_file = os.path.join(JOBS_DIR, f"{job_id}.json")

    job_data = {
        "job_id": job_id,
        "repo_url": req.repo_url,
        "status": "queued",
        "output_url": None,
        "error": None,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }

    # write the complete job JSON (prevent empty-file crashes)
    with open(job_file, "w") as f:
        json.dump(job_data, f, indent=2)

    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def job_status(job_id: str):
    job_file = os.path.join(JOBS_DIR, f"{job_id}.json")
    if not os.path.exists(job_file):
        return {"error": "job not found"}

    try:
        with open(job_file, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        # If file is temporarily invalid, return intermediate status
        return {"job_id": job_id, "status": "unknown", "note": "job file invalid or being written"}
    return data
