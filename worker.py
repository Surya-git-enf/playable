import os
import time
import redis

REDIS_URL = os.getenv("REDIS_URL")

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

BASE_DIR = "/app/builds"

def run_job(job_id: str):
    # 1. Make sure builds folder exists
    os.makedirs(BASE_DIR, exist_ok=True)

    # 2. Create job folder
    job_dir = f"{BASE_DIR}/work_{job_id}"
    os.makedirs(job_dir, exist_ok=True)

    # 3. Update status
    r.hset(job_id, mapping={
        "status": "running",
        "output": ""
    })

    # 4. Simulate long task
    time.sleep(5)

    # 5. Write output
    output_file = f"{job_dir}/result.txt"
    with open(output_file, "w") as f:
        f.write("Job completed successfully")

    # 6. Save output + status
    r.hset(job_id, mapping={
        "status": "completed",
        "output": output_file
    })
