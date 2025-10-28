from fastapi import FastAPI
from pydantic import BaseModel
import random

app = FastAPI(title="Mock Unreal Automation 🧱")

class BuildRequest(BaseModel):
    script: str
    build_type: str

@app.post("/build")
def build_unreal(req: BuildRequest):
    build_id = random.randint(1000, 9999)
    base_url = "https://cdn.example.com/builds"
    
    return {
        "build_id": build_id,
        "webgl_url": f"{base_url}/{build_id}/index.html",
        "apk_url": f"{base_url}/{build_id}/game.apk",
        "message": f"Build simulated successfully for {req.build_type.upper()} 🎮"
    }
