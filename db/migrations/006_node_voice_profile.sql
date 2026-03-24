-- =============================================================================
-- Migration 006 – Node Voice Profile
-- =============================================================================
-- Adds a voice_id column to node_registry so each Ollama node can carry a
-- unique text-to-speech voice identity.
--
-- voice_id    – An edge-tts compatible voice name (e.g. "en-US-GuyNeural",
--               "en-GB-RyanNeural", "en-AU-NatashaNeural").
--               NULL means the node uses the bot's global default voice.
--
-- The Discord bot reads this value from SubAgentResult.voice_id (populated
-- by SubAgentDispatcher from the node that handled the task) to select
-- which TTS voice to use when piping NPC dialogue into the voice channel.
-- Each Ollama node therefore has a persistent vocal persona across the
-- entire campaign — "the Synology sounds like a gruff northerner,
-- the Gaming Rig sounds like a whispering schemer."

ALTER TABLE node_registry
    ADD COLUMN IF NOT EXISTS voice_id TEXT NULL;

-- Seed a handful of default voices for known node types.
-- Operators can override these via the White Portal node editor.
COMMENT ON COLUMN node_registry.voice_id IS
    'edge-tts voice name for TTS puppeteering. NULL = use bot global default. '
    'Examples: en-US-GuyNeural, en-GB-RyanNeural, en-AU-NatashaNeural, '
    'en-US-AriaNeural, en-US-EricNeural, en-IE-ConnorNeural.';
