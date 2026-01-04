import os
import time
import json
import subprocess
import redis
from datetime import datetime

REDIS_URL = os.environ["REDIS_URL"]
r = redis.from_url(REDIS_URL, decode_responses=True)

BUILD_DIR = "/app/builds"
os.makedirs(BUILD_DIR, exist_ok=True)

print("Worker started - waiting for jobs...")

while True:
    job_id = r.lpop("job_queue")
    if not job_id:
        time.sleep(2)
        continue

    job_key = f"job:{job_id}"
    job = json.loads(r.get(job_key))

    try:
        r.hset(job_key, mapping={
            "status": "building",
            "started_at": datetime.utcnow().isoformat() + "Z"
        })

        repo = job["repo_url"]
        workdir = os.path.join(BUILD_DIR, f"work_{job_id}")

        # Clone repo
        subprocess.run(
            ["git", "clone", repo, workdir],
            check=True
        )

        # ✅ CREATE WEB EXPORT DIR (THIS FIXES YOUR ERROR)
        web_dir = os.path.join(workdir, "web")
        os.makedirs(web_dir, exist_ok=True)

        # Export WebGL
        subprocess.run(
            [
                "godot",
                "--headless",
                "--path", workdir,
                "--export-release",
                "Web",
                os.path.join(web_dir, "index.html")
            ],
            check=True
        )

        r.hset(job_key, mapping={
            "status": "done",
            "output_url": f"/outputs/{job_id}/index.html"
        })

    except Exception as e:
        r.hset(job_key, mapping={
            "status": "failed",
            "error": str(e)
        })
