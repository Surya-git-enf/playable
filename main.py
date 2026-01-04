from fastapi import FastAPI
from pydantic import BaseModel
import uuid, json, os
import redis
from datetime import datetime

REDIS_URL = os.environ["REDIS_URL"]
r = redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI()

class BuildRequest(BaseModel):
    repo_url: str

@app.post("/build")
def start_build(req: BuildRequest):
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    job_data = {
        "job_id": job_id,
        "repo_url": req.repo_url,
        "status": "queued",
        "output_url": "",
        "error": "",
        "created_at": datetime.utcnow().isoformat() + "Z"
    }

    # ✅ Save job data
    r.set(f"job:{job_id}", json.dumps(job_data))

    # ✅ PUSH job into queue (THIS WAS MISSING)
    r.rpush("job_queue", job_id)

    return {
        "job_id": job_id,
        "status": "queued"
    }

@app.get("/status/{job_id}")
def job_status(job_id: str):
    data = r.get(f"job:{job_id}")
    if not data:
        return {"error": "job not found"}
    return json.loads(data)
