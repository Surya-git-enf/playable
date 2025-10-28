from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
import os

app = FastAPI()

# Use persistent directory (Render allows temporary storage during runtime)
BASE_DIR = "/tmp/generated_games"
os.makedirs(BASE_DIR, exist_ok=True)

class ScriptInput(BaseModel):
    script: str

@app.post("/build")
async def build_game(data: ScriptInput):
    # Create filename from prompt
    game_name = data.script.replace(" ", "_").lower()[:30]
    file_path = os.path.join(BASE_DIR, f"{game_name}.html")

    # Generate simple playable HTML
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{data.script}</title>
        <style>
            body {{
                margin: 0;
                height: 100vh;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                background: linear-gradient(135deg, #111, #333);
                color: white;
                font-family: Arial;
            }}
            .shape {{
                width: 100px;
                height: 100px;
                background: {'blue' if 'blue' in data.script.lower() else 'red'};
                border-radius: {'50%' if 'circle' in data.script.lower() else '0'};
                animation: { 'spin 1s linear infinite' if 'fast' in data.script.lower() else 'spin 3s linear infinite' };
            }}
            @keyframes spin {{
                from {{ transform: rotate(0deg); }}
                to {{ transform: rotate(360deg); }}
            }}
        </style>
    </head>
    <body>
        <h2>🎮 Game Preview</h2>
        <div class="shape"></div>
        <p>{data.script}</p>
    </body>
    </html>
    """

    # Save HTML to file
    with open(file_path, "w") as f:
        f.write(html_content)

    base_url = "https://playable-36ab.onrender.com"
    webgl_url = f"{base_url}/preview/{game_name}"
    apk_url = f"{base_url}/download/{game_name}.apk"

    return JSONResponse({
        "webgl_url": webgl_url,
        "apk_url": apk_url
    })

@app.get("/preview/{game_name}")
async def preview_game(game_name: str):
    file_path = os.path.join(BASE_DIR, f"{game_name}.html")
    if not os.path.exists(file_path):
        return HTMLResponse("<h2>❌ Game not found! (File missing)</h2>", status_code=404)
    with open(file_path, "r") as f:
        return HTMLResponse(content=f.read(), status_code=200)

@app.get("/download/{apk_name}")
async def download_apk(apk_name: str):
    return JSONResponse({
        "message": f"✅ APK {apk_name} generated (placeholder).",
        "note": "In future: Unreal Engine .apk export here."
    })
