from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import uuid
import redis
import os
from worker import run_job

app = FastAPI()

REDIS_URL = os.getenv("REDIS_URL")
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


class BuildRequest(BaseModel):
    repo_url: str


@app.get("/")
def home():
    return {"status": "Server running 🚀"}


@app.post("/build")
def start_build(req: BuildRequest, background_tasks: BackgroundTasks):
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    r.hset(job_id, mapping={
        "status": "queued",
        "repo_url": req.repo_url,
        "output_url": "",
        "error": ""
    })

    background_tasks.add_task(run_job, job_id, req.repo_url)

    return {
        "job_id": job_id,
        "status": "queued"
    }


@app.get("/status/{job_id}")
def job_status(job_id: str):
    if not r.exists(job_id):
        return {"error": "Job not found"}

    return r.hgetall(job_id)
