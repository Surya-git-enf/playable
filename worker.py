import os
import time
import redis
import shutil
from git import Repo

REDIS_URL = os.getenv("REDIS_URL")
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

BASE_DIR = "/app/builds"

def run_job(job_id: str, repo_url: str):
    try:
        # 1️⃣ Create builds directory
        os.makedirs(BASE_DIR, exist_ok=True)

        job_dir = f"{BASE_DIR}/work_{job_id}"
        os.makedirs(job_dir, exist_ok=True)

        # 2️⃣ Update status
        r.hset(job_id, mapping={
            "status": "cloning",
            "error": "",
            "output_url": ""
        })

        # 3️⃣ Clone repo
        Repo.clone_from(repo_url, job_dir)

        # 4️⃣ Simulate build (Godot export later)
        r.hset(job_id, "status", "building")
        time.sleep(5)

        # 5️⃣ Fake output
        output_path = f"{job_dir}/BUILD_SUCCESS.txt"
        with open(output_path, "w") as f:
            f.write("Game build successful 🎮")

        # 6️⃣ Done
        r.hset(job_id, mapping={
            "status": "completed",
            "output_url": output_path
        })

    except Exception as e:
        r.hset(job_id, mapping={
            "status": "failed",
            "error": str(e)
        })
