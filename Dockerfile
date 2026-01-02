# Dockerfile (robust Godot download + FastAPI)
FROM python:3.11-slim

# install system deps
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    wget curl unzip ca-certificates libx11-6 libxcursor1 libxrandr2 libxinerama1 libxi6 libgl1 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp

# list of Godot versions to try (first that works is used)
ENV GODOT_VERSIONS="4.2.1 4.2.0 4.1.6 4.1.5"
# base download pattern (we will fill version token into URL)
ENV GODOT_BASE_URL="https://downloads.tuxfamily.org/godotengine"

# helper: try downloading each in order and install to /usr/local/bin/godot
RUN set -euo pipefail; \
    for v in $GODOT_VERSIONS; do \
      echo "Trying Godot version: $v"; \
      # build filename and url (common naming pattern)
      fname="Godot_v${v}-stable_linux.x86_64.zip"; \
      url="${GODOT_BASE_URL}/${v}/${fname}"; \
      echo " -> URL: $url"; \
      # attempt download (curl will fail on HTTP error)
      if curl -fSL --retry 3 --retry-delay 2 -o "$fname" "$url"; then \
         echo "Downloaded $fname (size: $(stat -c%s $fname) bytes)"; \
         if [ "$(stat -c%s $fname)" -lt 1000 ]; then \
           echo "File too small, skipping"; rm -f "$fname"; continue; fi; \
         unzip -q "$fname"; \
         # unzip typically creates binary named "Godot_vX.Y.Z-stable_linux.x86_64"
         # try expected names, then move to /usr/local/bin/godot
         bin_candidate="./Godot_v${v}-stable_linux.x86_64"; \
         if [ -f "$bin_candidate" ]; then \
           mv "$bin_candidate" /usr/local/bin/godot && chmod +x /usr/local/bin/godot; \
           echo "Installed godot from $fname -> /usr/local/bin/godot"; \
           break; \
         else \
           # try to find any extracted file that looks like godot binary
           bin_found=$(unzip -Z1 "$fname" | grep -E "Godot|godot" | head -n1 || true); \
           if [ -n "$bin_found" ]; then \
             unzip -p "$fname" "$bin_found" > /usr/local/bin/godot && chmod +x /usr/local/bin/godot; \
             echo "Installed godot from $bin_found inside $fname"; break; \
           else \
             echo "No binary found inside $fname; skipping"; rm -f "$fname"; continue; \
           fi; \
         fi; \
      else \
         echo "Download failed for $url (HTTP error)"; rm -f "$fname" || true; continue; \
      fi; \
    done; \
    # final check
    if [ ! -x /usr/local/bin/godot ]; then echo "ERROR: godot binary not installed"; ls -la; exit 1; fi

# install export templates (optional) - adjust version folder if needed
# Note: export templates url pattern may differ across releases. If this fails, you can remove it and upload templates manually.
RUN mkdir -p /root/.local/share/godot/export_templates \
  && echo "Note: export templates should be added manually if needed"

WORKDIR /app

# python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app
COPY . .

ENV GODOT_BIN=/usr/local/bin/godot
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
