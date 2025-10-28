from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.responses import JSONResponse, HTMLResponse
import os

app = FastAPI()

# Storage path (Render ephemeral storage)
OUTPUT_DIR = "generated_games"
os.makedirs(OUTPUT_DIR, exist_ok=True)

class ScriptInput(BaseModel):
    script: str

@app.post("/build")
async def build_game(data: ScriptInput):
    game_name = data.script.replace(" ", "_").lower()[:20]
    html_file = os.path.join(OUTPUT_DIR, f"{game_name}.html")

    # Simple "fake game" HTML based on prompt
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
                align-items: center;
                justify-content: center;
                background: linear-gradient(135deg, #1a1a1a, #444);
                color: white;
                font-family: Arial;
            }}
            .cube {{
                width: 100px;
                height: 100px;
                background: red;
                animation: spin 3s linear infinite;
            }}
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
        </style>
    </head>
    <body>
        <div>
            <h2>🎮 Game Preview</h2>
            <div class="cube"></div>
            <p>{data.script}</p>
        </div>
    </body>
    </html>
    """

    # Write the HTML file
    with open(html_file, "w") as f:
        f.write(html_content)

    # Generate playable URL (served by this same FastAPI)
    webgl_url = f"/preview/{game_name}"
    apk_url = f"/download/{game_name}.apk"

    return JSONResponse({
        "webgl_url": webgl_url,
        "apk_url": apk_url
    })

@app.get("/preview/{name}")
async def preview_game(name: str):
    html_path = os.path.join(OUTPUT_DIR, f"{name}.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h2>❌ Game not found!</h2>", status_code=404)
    with open(html_path, "r") as f:
        return HTMLResponse(content=f.read())

@app.get("/download/{apk_name}")
async def download_apk(apk_name: str):
    # Placeholder link - in real case, build the APK using Unreal
    return JSONResponse({
        "message": f"✅ APK for {apk_name} generated (placeholder).",
        "download_hint": "APK building via Unreal integration coming soon."
    })
