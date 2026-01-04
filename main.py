from fastapi import FastAPI, BackgroundTasks
import uuid
import redis
import os
from worker import run_job

app = FastAPI()

REDIS_URL = os.getenv("REDIS_URL")
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


@app.get("/")
def home():
    return {"status": "Server running 🚀"}


@app.post("/start-job")
def start_job(background_tasks: BackgroundTasks):
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    # save initial status
    r.hset(job_id, mapping={
        "status": "queued",
        "output": ""
    })

    background_tasks.add_task(run_job, job_id)

    return {
        "job_id": job_id,
        "status": "queued"
    }


@app.get("/job-status/{job_id}")
def job_status(job_id: str):
    if not r.exists(job_id):
        return {"error": "Job not found"}

    return r.hgetall(job_id)
