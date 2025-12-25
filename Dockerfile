FROM python:3.10-slim

# Install system deps
RUN apt-get update && apt-get install -y \
    wget unzip libx11-6 libxcursor1 libxrandr2 libxinerama1 libxi6 libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Download Godot headless
RUN wget https://downloads.tuxfamily.org/godotengine/4.2.1/Godot_v4.2.1-stable_linux.x86_64.zip \
    && unzip Godot_v4.2.1-stable_linux.x86_64.zip \
    && mv Godot_v4.2.1-stable_linux.x86_64 /usr/local/bin/godot \
    && chmod +x /usr/local/bin/godot

# Install export templates
RUN mkdir -p /root/.local/share/godot/export_templates/4.2.1.stable \
    && wget https://downloads.tuxfamily.org/godotengine/4.2.1/Godot_v4.2.1-stable_export_templates.tpz \
    && unzip Godot_v4.2.1-stable_export_templates.tpz -d /root/.local/share/godot/export_templates/4.2.1.stable

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
