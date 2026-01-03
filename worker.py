import json, os, time, subprocess, shutil

JOBS_DIR = "jobs"
BUILD_DIR = "builds"

os.makedirs(BUILD_DIR, exist_ok=True)

while True:
    for file in os.listdir(JOBS_DIR):
        if not file.endswith(".json"):
            continue

        path = os.path.join(JOBS_DIR, file)
        with open(path) as f:
            job = json.load(f)

        if job["status"] != "queued":
            continue

        job["status"] = "building"
        with open(path, "w") as f:
            json.dump(job, f)

        repo = job["repo_url"]
        job_id = job["job_id"]
        workdir = os.path.join(BUILD_DIR, job_id)

        try:
            subprocess.run(["git", "clone", repo, workdir], check=True)

            subprocess.run([
                "godot",
                "--headless",
                "--path", workdir,
                "--export-release", "Web", f"{workdir}/web/index.html"
            ], check=True)

            job["status"] = "done"
            job["output_url"] = f"/static/{job_id}/index.html"

        except Exception as e:
            job["status"] = "failed"
            job["error"] = str(e)

        with open(path, "w") as f:
            json.dump(job, f)

    time.sleep(5)
