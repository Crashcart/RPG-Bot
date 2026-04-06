"""
Piper TTS HTTP Server
======================
Minimal Flask service that wraps the piper-tts Python library and exposes a
REST API consumed by the orchestrator's PiperClient.

Endpoints
---------
POST /api/tts
    JSON body: {"text": str, "voice_model": str, "speed_scale": float,
                "noise_scale": float, "noise_w": float}
    Returns: audio/wav bytes

GET /api/voices
    Returns: JSON list of available voice model names (filenames under PIPER_MODELS_DIR
             without the .onnx extension)

GET /health
    Returns: {"status": "ok", "models_loaded": int}

Environment Variables
---------------------
PIPER_MODELS_DIR     Directory containing .onnx voice model files (default: /models)
PIPER_DEFAULT_MODEL  Fallback voice model name when none is specified (default: en_US-lessac-medium)
PIPER_HTTP_PORT      Port to bind (default: 5500)

Voice Model Setup
-----------------
Download Piper voice models from:
  https://github.com/rhasspy/piper/releases/tag/2023.11.14-2

Each model requires two files:
  <name>.onnx        — the neural network weights
  <name>.onnx.json   — the model configuration

Place both files in PIPER_MODELS_DIR (mounted volume: ./piper-models:/models).
"""

from __future__ import annotations

import io
import logging
import os
import wave

from flask import Flask, Response, jsonify, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

_MODELS_DIR    = os.environ.get("PIPER_MODELS_DIR",    "/models")
_DEFAULT_MODEL = os.environ.get("PIPER_DEFAULT_MODEL", "en_US-lessac-medium")

# Cache loaded PiperVoice objects so models are only parsed from disk once.
_voice_cache: dict[str, object] = {}


def _load_voice(model_name: str):
    """
    Load and cache a PiperVoice from disk.

    Returns the PiperVoice object or None if the model file is not found.
    """
    if model_name in _voice_cache:
        return _voice_cache[model_name]

    onnx_path = os.path.join(_MODELS_DIR, f"{model_name}.onnx")
    if not os.path.exists(onnx_path):
        logger.warning("Piper model not found: %s", onnx_path)
        return None

    try:
        from piper.tts import PiperVoice  # type: ignore[import-untyped]
        voice = PiperVoice.load(onnx_path)
        _voice_cache[model_name] = voice
        logger.info("Loaded Piper model: %s", model_name)
        return voice
    except Exception as exc:
        logger.error("Failed to load Piper model %s: %s", model_name, exc)
        return None


@app.post("/api/tts")
def synthesize():
    """
    Synthesize speech from text using a Piper voice model.

    Request body (JSON):
      text         (str,   required) — text to synthesize
      voice_model  (str,   optional) — model name under PIPER_MODELS_DIR; falls back to
                                       PIPER_DEFAULT_MODEL env var
      speed_scale  (float, optional, default 1.0) — playback speed (>1 = faster)
      noise_scale  (float, optional, default 0.667) — phoneme duration variability
      noise_w      (float, optional, default 0.8)   — pitch variability

    Returns:
      audio/wav bytes on success
      JSON error with 400/404/500 on failure
    """
    body = request.get_json(silent=True) or {}
    text        = (body.get("text") or "").strip()
    model_name  = (body.get("voice_model") or _DEFAULT_MODEL).strip()
    speed_scale = float(body.get("speed_scale", 1.0))
    noise_scale = float(body.get("noise_scale", 0.667))
    noise_w     = float(body.get("noise_w",     0.8))

    if not text:
        return jsonify({"error": "text is required"}), 400

    voice = _load_voice(model_name)
    if voice is None:
        # Attempt fallback to default model
        if model_name != _DEFAULT_MODEL:
            logger.warning(
                "Model '%s' not available — falling back to '%s'.", model_name, _DEFAULT_MODEL
            )
            voice = _load_voice(_DEFAULT_MODEL)
        if voice is None:
            return jsonify({"error": f"Model not found: {model_name}"}), 404

    try:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            # length_scale is the inverse of speed_scale (>1 = slower)
            voice.synthesize(
                text,
                wf,
                length_scale=1.0 / max(speed_scale, 0.1),
                noise_scale=noise_scale,
                noise_w=noise_w,
            )
        buf.seek(0)
        return Response(buf.read(), mimetype="audio/wav", status=200)
    except Exception as exc:
        logger.error("Synthesis failed (model=%s): %s", model_name, exc)
        return jsonify({"error": f"Synthesis failed: {exc}"}), 500


@app.get("/api/voices")
def list_voices():
    """List all available Piper voice model names (without .onnx extension)."""
    try:
        voices = sorted(
            f.removesuffix(".onnx")
            for f in os.listdir(_MODELS_DIR)
            if f.endswith(".onnx")
        )
    except FileNotFoundError:
        voices = []
    return jsonify(voices)


@app.get("/health")
def health():
    """Healthcheck endpoint — also reports number of loaded (cached) models."""
    return jsonify({"status": "ok", "models_loaded": len(_voice_cache)})


if __name__ == "__main__":
    port = int(os.environ.get("PIPER_HTTP_PORT", "5500"))
    logger.info("Piper TTS server starting on port %d, models dir: %s", port, _MODELS_DIR)
    app.run(host="0.0.0.0", port=port, debug=False)
