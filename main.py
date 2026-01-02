import os
import uuid
import subprocess
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from git import Repo
from fastapi.staticfiles import StaticFiles
import subprocess
print(subprocess.check_output(["godot", "--version"]).decode())

GODOT_BIN = os.getenv("GODOT_BIN", "godot")
BUILD_ROOT = os.getenv("BUILD_ROOT", "/tmp/builds")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000")

os.makedirs(BUILD_ROOT, exist_ok=True)

app = FastAPI()

app.mount("/builds", StaticFiles(directory=BUILD_ROOT), name="builds")

class BuildRequest(BaseModel):
    repo_url: str
    game_name: str = "Auto Game"

@app.post("/build")
def build_game(req: BuildRequest):
    build_id = str(uuid.uuid4())[:8]
    work_dir = f"{BUILD_ROOT}/{build_id}"
    export_dir = f"{work_dir}/export"

    try:
        Repo.clone_from(req.repo_url, work_dir)

        os.makedirs(export_dir, exist_ok=True)

        # OPTIONAL: modify game logic dynamically
        main_gd = f"{work_dir}/scripts/main.gd"
        if os.path.exists(main_gd):
            with open(main_gd, "a") as f:
                f.write("\nprint('Game built by backend')\n")

        # Run Godot export
        subprocess.run(
            [
                GODOT_BIN,
                "--headless",
                "--path", work_dir,
                "--export-release", "Web",
                f"{export_dir}/index.html"
            ],
            check=True
        )

        play_url = f"{PUBLIC_URL}/builds/{build_id}/export/index.html"

        return {
            "status": "success",
            "play_url": play_url
        }

    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"Godot build failed: {e}")
    except Exception as e:
        raise HTTPException(500, str(e))
