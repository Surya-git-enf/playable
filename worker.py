# worker.py
import json
import os
import time
import subprocess
import shutil

JOBS_DIR = os.getenv("JOBS_DIR", "/app/jobs")
BUILD_DIR = os.getenv("BUILD_DIR", "/app/builds")
os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(BUILD_DIR, exist_ok=True)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "4"))

def safe_load_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return None

def safe_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

print("Worker started. Watching jobs in:", JOBS_DIR)

while True:
    try:
        files = [f for f in os.listdir(JOBS_DIR) if f.endswith(".json")]
    except FileNotFoundError:
        files = []

    for filename in files:
        path = os.path.join(JOBS_DIR, filename)

        job = safe_load_json(path)
        if not job:
            # file empty or being written; skip this cycle
            continue

        # Only process queued jobs
        if job.get("status") != "queued":
            continue

        # Mark building (write atomically)
        job["status"] = "building"
        safe_write_json(path, job)

        repo = job.get("repo_url")
        job_id = job.get("job_id")
        workdir = os.path.join(BUILD_DIR, f"work_{job_id}")
        export_tmp_dir = os.path.join(workdir, "web")
        final_output_dir = os.path.join(BUILD_DIR, job_id)

        # cleanup if leftover
        if os.path.exists(workdir):
            shutil.rmtree(workdir, ignore_errors=True)
        os.makedirs(workdir, exist_ok=True)

        try:
            # Clone repo
            subprocess.run(["git", "clone", "--depth", "1", repo, workdir], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # Ensure export directory exists
            os.makedirs(os.path.dirname(export_tmp_dir), exist_ok=True)

            # Run Godot export (adjust "Web" if your export preset name differs)
            # This writes index.html (and .wasm/.js) inside export_tmp_dir
            proc = subprocess.run([
                "godot",
                "--headless",
                "--path", workdir,
                "--export-release", "Web", f"{export_tmp_dir}/index.html"
            ], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            if proc.returncode != 0:
                raise RuntimeError(f"Godot export failed: rc={proc.returncode} stdout={proc.stdout} stderr={proc.stderr}")

            # Move/copy export to public static dir
            if os.path.exists(final_output_dir):
                shutil.rmtree(final_output_dir, ignore_errors=True)
            shutil.copytree(export_tmp_dir, final_output_dir)

            # Update job info
            job["status"] = "done"
            job["output_url"] = f"/static/{job_id}/index.html"
            job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        except Exception as e:
            job["status"] = "failed"
            job["error"] = str(e)

        # Save job file (atomic)
        safe_write_json(path, job)

    time.sleep(POLL_INTERVAL)
