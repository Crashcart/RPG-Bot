-- Migration 013: Inference engine settings + SillyTavern integration
-- Run: psql -U ironclad -d ironclad -f db/migrations/013_inference_settings.sql

-- ── Inference provider settings ─────────────────────────────────────────────
-- adjudication_provider: which engine handles Phase 2 mechanical resolution.
-- cloud_provider: which engine handles Phase 4 narrative prose.
-- All values can be changed at runtime via White Portal → Settings.

INSERT INTO global_settings (key, value) VALUES
  -- Cloud adjudication provider (ollama = local; groq/openrouter/together/sillytavern = cloud)
  ('adjudication_provider',  '"ollama"'),
  -- Cloud narration storyteller (gemini/claude/sillytavern)
  ('cloud_provider',         '"gemini"'),

  -- ── SillyTavern ───────────────────────────────────────────────────────────
  -- External SillyTavern instance. Not part of the install.
  -- Leave sillytavern_url empty to keep it disabled.
  ('sillytavern_url',        '""'),
  ('sillytavern_model',      '""'),
  ('sillytavern_api_key',    '""'),

  -- ── Cloud adjudication API keys ───────────────────────────────────────────
  ('groq_api_key',           '""'),
  ('groq_model',             '"llama-3.3-70b-versatile"'),
  ('openrouter_api_key',     '""'),
  ('openrouter_model',       '"meta-llama/llama-3.3-70b-instruct"'),
  ('together_api_key',       '""'),
  ('together_model',         '"meta-llama/Llama-3.3-70B-Instruct-Turbo"'),

  -- ── Voice / TTS ───────────────────────────────────────────────────────────
  ('tts_provider',           '"edge_tts"'),   -- edge_tts | elevenlabs | openai_tts
  ('voice_idle_timeout_s',   '300'),

  -- ── Image generation ─────────────────────────────────────────────────────
  ('image_gen_backend',      '"disabled"'),   -- disabled | comfyui | stability_ai | dalle3
  ('comfyui_url',            '"http://comfyui:8188"'),
  ('stability_ai_api_key',   '""'),

  -- ── Music (Lyria 3) ───────────────────────────────────────────────────────
  ('music_model',            '"lyria-3-clip-preview"'),  -- lyria-3-clip-preview | lyria-3-pro-preview | lavalink

  -- ── ElevenLabs ────────────────────────────────────────────────────────────
  ('elevenlabs_api_key',     '""')

ON CONFLICT (key) DO NOTHING;
