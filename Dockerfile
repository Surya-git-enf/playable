FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl unzip git ca-certificates \
    libfontconfig1 libx11-6 libxcursor1 libxrandr2 libxi6 libgl1 libgl1-mesa-dri libasound2 libpulse0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN curl -fSL https://github.com/godotengine/godot/releases/download/4.1.3-stable/Godot_v4.1.3-stable_linux.x86_64.zip -o godot.zip \
  && unzip godot.zip \
  && mv Godot_v4.1.3-stable_linux.x86_64 /usr/local/bin/godot \
  && chmod +x /usr/local/bin/godot \
  && rm godot.zip

# verify
RUN /usr/local/bin/godot --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=8000
EXPOSE 8000

# Run worker in background and API in foreground
CMD ["sh", "-c", "python worker.py & uvicorn main:app --host 0.0.0.0 --port $PORT"]
