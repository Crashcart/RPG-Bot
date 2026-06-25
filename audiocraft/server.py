"""Minimal AudioGen HTTP server wrapping Meta's AudioCraft library."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ASSETS_DIR = Path(os.getenv("ASSETS_DIR", "/app/assets/audio"))
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

MODEL_NAME = os.getenv("AUDIOCRAFT_MODEL", "facebook/audiogen-medium")
MAX_DURATION = int(os.getenv("MAX_DURATION", "30"))

app = FastAPI(title="AudioGen Server")
_model = None


def _get_model():
    global _model
    if _model is None:
        from audiocraft.models import AudioGen
        _model = AudioGen.get_pretrained(MODEL_NAME)
    return _model


class GenerateRequest(BaseModel):
    prompt: str
    duration: int = 10


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/generate")
def generate(req: GenerateRequest):
    duration = min(req.duration, MAX_DURATION)
    try:
        model = _get_model()
        model.set_generation_params(duration=duration)
        wav = model.generate([req.prompt])
        import torchaudio
        sig = hashlib.sha256(f"{req.prompt}:{duration}".encode()).hexdigest()[:16]
        filename = f"{sig}.wav"
        path = ASSETS_DIR / filename
        if not path.exists():
            torchaudio.save(str(path), wav[0].cpu(), sample_rate=model.sample_rate)
        return {"filename": filename}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
