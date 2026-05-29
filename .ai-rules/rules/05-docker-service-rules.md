## Docker and Service Rules

- Service names follow the `aetheris_*` network convention defined in `docker-compose.yml`.
- New services must join `aetheris_net`. Services needing persistent storage also join `aetheris_store`.
- Health checks are required for every new service added to `docker-compose.yml`.
- Never hardcode ports in application code — read from environment variables.
- External services (SillyTavern, custom Ollama nodes, remote APIs) are configured via
  `system_settings` and are **never** added to `docker-compose.yml`.
- Compose override files for each tier (`compose.alpha.yml`, `compose.beta.yml`, `compose.prod.yml`)
  layer on top of the base `docker-compose.yml` — never duplicate full service definitions.
