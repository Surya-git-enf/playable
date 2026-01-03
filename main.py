from fastapi import FastAPI
from pydantic import BaseModel
import uuid, json, os

app = FastAPI()

JOBS_DIR = "jobs"
os.makedirs(JOBS_DIR, exist_ok=True)

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
        "output_url": None
    }

    with open(job_file, "w") as f:
        json.dump(job_data, f)

    return {
        "job_id": job_id,
        "status": "queued"
    }

@app.get("/status/{job_id}")
def job_status(job_id: str):
    job_file = os.path.join(JOBS_DIR, f"{job_id}.json")
    if not os.path.exists(job_file):
        return {"error": "job not found"}

    with open(job_file) as f:
        return json.load(f)
