# main.py
import os, uuid, json
from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
import redis
from datetime import datetime

# Redis connection from env
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.from_url(REDIS_URL, decode_responses=True)

JOBS_QUEUE = "jobs_queue"   # Redis list
STATUS_PREFIX = "job:"      # Redis hash prefix

BUILD_DIR = os.getenv("BUILD_DIR", "/app/builds")
os.makedirs(BUILD_DIR, exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory=BUILD_DIR), name="static")

class BuildRequest(BaseModel):
    repo_url: str

@app.post("/build")
def start_build(req: BuildRequest):
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    job_key = f"{STATUS_PREFIX}{job_id}"
    job_data = {
        "job_id": job_id,
        "repo_url": req.repo_url,
        "status": "queued",
        "output_url": "",
        "error": "",
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    # store status (hash) and push to queue
    r.hset(job_key, mapping=job_data)
    # push a serialized payload to the queue
    r.lpush(JOBS_QUEUE, json.dumps(job_data))
    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def status(job_id: str):
    job_key = f"{STATUS_PREFIX}{job_id}"
    if not r.exists(job_key):
        return {"error": "job not found"}
    return r.hgetall(job_key)

@app.get("/debug")
def debug():
    return {"status":"Good"}
