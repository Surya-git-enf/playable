# main.py
from fastapi import FastAPI
from pydantic import BaseModel
import requests

app = FastAPI()

UNREAL_AUTOMATION_URL = "https://your-unreal-automation-endpoint.com/build"

class GameScript(BaseModel):
    game_script: str

@app.post("/game")
def create_game(script: GameScript):
    # 1️⃣ Send script to Unreal Engine automation endpoint
    payload = {"script": script.game_script}
    
    response = requests.post(UNREAL_AUTOMATION_URL, json=payload)
    
    if response.status_code != 200:
        return {"error": "Unreal build failed", "details": response.text}
    
    # 2️⃣ Expected Unreal response (example)
    # {"webgl_url": "https://netlify.app/mygame", "apk_url": "https://cdn.com/mygame.apk"}
    result = response.json()
    
    return {
        "message": "Game built successfully!",
        "webgl_preview_url": result.get("webgl_url"),
        "apk_download_url": result.get("apk_url")
    }
