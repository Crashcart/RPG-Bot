FROM python:3.11-slim

# ── System deps for onnxruntime (piper-tts dependency) ───────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Voice model files are expected at /models (mounted volume).
# Download .onnx + .onnx.json pairs from:
#   https://github.com/rhasspy/piper/releases/tag/2023.11.14-2
RUN mkdir -p /models

EXPOSE 5500

CMD ["python", "server.py"]
