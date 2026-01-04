# worker.py
import os, json, time, subprocess, shutil
import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.from_url(REDIS_URL, decode_responses=True)

JOBS_QUEUE = "jobs_queue"
STATUS_PREFIX = "job:"
BUILD_ROOT = os.getenv("BUILD_DIR", "/app/builds")
os.makedirs(BUILD_ROOT, exist_ok=True)

POLL_TIMEOUT = int(os.getenv("BRPOP_TIMEOUT", "5"))

print("Worker started - waiting for jobs...")

while True:
    res = r.brpop(JOBS_QUEUE, timeout=POLL_TIMEOUT)
    if not res:
        continue
    _, payload = res
    try:
        job = json.loads(payload)
    except Exception as e:
        print("Invalid job payload:", e)
        continue

    job_id = job["job_id"]
    job_key = f"{STATUS_PREFIX}{job_id}"
    # mark building
    r.hset(job_key, mapping={
        "status": "building",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    })

    repo = job["repo_url"]
    workdir = os.path.join(BUILD_ROOT, f"work_{job_id}")
    export_tmp = os.path.join(workdir, "web")
    final_out = os.path.join(BUILD_ROOT, job_id)

    # cleanup previous
    shutil.rmtree(workdir, ignore_errors=True)

    try:
        print(f"[{job_id}] Cloning {repo} -> {workdir}")
        subprocess.run(["git", "clone", "--depth", "1", repo, workdir], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Ensure export directory exists
        os.makedirs(os.path.dirname(export_tmp), exist_ok=True)

        print(f"[{job_id}] Running Godot export...")
        proc = subprocess.run([
            "godot",
            "--headless",
            "--path", workdir,
            "--export-release", "Web", f"{export_tmp}/index.html"
        ], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            r.hset(job_key, mapping={"status": "failed", "error": f"Godot failed: {err}"})
            print(f"[{job_id}] Godot failed: {err}")
            continue

        # copy exported files to public static dir
        if os.path.exists(final_out):
            shutil.rmtree(final_out, ignore_errors=True)
        shutil.copytree(export_tmp, final_out)

        output_url = f"/static/{job_id}/index.html"
        r.hset(job_key, mapping={"status": "done", "output_url": output_url, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        print(f"[{job_id}] Done -> {output_url}")

    except subprocess.CalledProcessError as e:
        err = e.stderr if hasattr(e, "stderr") else str(e)
        r.hset(job_key, mapping={"status": "failed", "error": err})
        print(f"[{job_id}] CalledProcessError: {err}")
    except Exception as e:
        r.hset(job_key, mapping={"status": "failed", "error": str(e)})
        print(f"[{job_id}] Unexpected error: {e}")
