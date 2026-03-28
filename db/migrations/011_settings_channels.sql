-- Migration 011: Seed runtime-configurable system settings
-- These values are managed via the White Portal → Settings page.
-- ON CONFLICT DO NOTHING preserves any values already set by an operator.

INSERT INTO system_settings (key, value) VALUES
  ('channel_map',         '{}'),
  ('admin_role_name',     '"GM"'),
  ('session_ttl_seconds', '3600'),
  ('gemini_model',        '"gemini-1.5-pro"'),
  ('ollama_model',        '"mistral:7b-instruct"'),
  ('claude_model',        '"claude-sonnet-4-6"'),
  ('cloud_provider',      '"gemini"'),
  ('gemini_api_key',      '""'),
  ('claude_api_key',      '""')
ON CONFLICT (key) DO NOTHING;
