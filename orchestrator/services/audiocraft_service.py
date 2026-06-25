"""Adaptive Environmental Soundscaping — AudioCraft/AudioGen integration.

Track layout:
  Track 1 — TTS voice       (PiperTTSService / voice_manager)
  Track 2 — Ambient loop    (this service, looped via Lavalink)
  Track 3 — One-shot SFX    (this service, played once via Lavalink)

All failures are non-fatal (fail-open): callers receive None URLs when the
AudioCraft container is unavailable or returns an error.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

import aiohttp

from orchestrator.config import Settings
from orchestrator.schemas.payloads import SoundscapeCue

_log = logging.getLogger(__name__)

_WEATHER_KEYWORDS = {
    "rain", "raining", "storm", "stormy", "thunder", "lightning",
    "snow", "blizzard", "hail", "drizzle", "downpour", "fog", "mist",
    "wind", "gust", "breeze",
}
_MATERIAL_KEYWORDS = {
    "metal", "iron", "steel", "wood", "stone", "glass", "leather",
    "bone", "ice", "fire", "water", "mud", "gravel", "sand",
}
_ACTION_KEYWORDS = {
    "slam", "crash", "bang", "clang", "shatter", "creak", "groan",
    "drip", "splash", "crackle", "rumble", "whisper", "roar", "hiss",
    "thud", "clatter", "squeak", "snap", "crack",
}
_ENVIRONMENT_KEYWORDS = {
    "tavern", "dungeon", "forest", "cave", "ocean", "river", "castle",
    "city", "market", "church", "temple", "underground", "wilderness",
    "swamp", "desert", "mountain", "space", "ship", "laboratory",
    "alley", "corridor", "hall", "vault",
}


@dataclass(frozen=True)
class AcousticContext:
    weather:     list[str] = field(default_factory=list)
    materials:   list[str] = field(default_factory=list)
    actions:     list[str] = field(default_factory=list)
    environment: list[str] = field(default_factory=list)

    @property
    def ambient_prompt(self) -> str:
        parts = []
        if self.environment:
            parts.append(f"{self.environment[0]} ambience")
        if self.weather:
            parts.append(", ".join(self.weather))
        return ", ".join(parts) if parts else "quiet interior ambience"

    @property
    def sfx_prompt(self) -> str | None:
        if not self.actions:
            return None
        base = self.actions[0]
        if self.materials:
            base = f"{self.materials[0]} {base}"
        return base

    @property
    def cache_key(self) -> str:
        signature = "|".join(sorted(self.environment + self.weather))
        return "audiocraft:ambient:" + hashlib.sha256(signature.encode()).hexdigest()[:16]

    @property
    def sfx_cache_key(self) -> str | None:
        sfx = self.sfx_prompt
        if sfx is None:
            return None
        return "audiocraft:sfx:" + hashlib.sha256(sfx.encode()).hexdigest()[:16]


def extract_acoustic_context(text: str) -> AcousticContext:
    """Extract acoustic keywords from GM narrative text."""
    words = set(re.findall(r"\b\w+\b", text.lower()))
    return AcousticContext(
        weather=sorted(words & _WEATHER_KEYWORDS),
        materials=sorted(words & _MATERIAL_KEYWORDS),
        actions=sorted(words & _ACTION_KEYWORDS),
        environment=sorted(words & _ENVIRONMENT_KEYWORDS),
    )


class AudioCraftService:
    """Manages AudioGen ambient loop and SFX generation with Redis caching."""

    def __init__(self, settings: Settings, redis=None) -> None:
        self._url = settings.audiocraft_url.rstrip("/")
        self._ambient_ttl = settings.audiocraft_ambient_ttl_seconds
        self._sfx_ttl = settings.audiocraft_sfx_ttl_seconds
        self._media_proxy_url = settings.media_proxy_url.rstrip("/")
        self._redis = redis
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        )

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def build_soundscape(
        self,
        narrative: str,
        location_seed: str = "",
    ) -> SoundscapeCue:
        """Derive a SoundscapeCue from GM narrative text."""
        ctx = extract_acoustic_context(narrative)
        ambient_url, from_cache = await self._get_ambient(ctx, location_seed)
        sfx_url = await self._get_sfx(ctx)
        return SoundscapeCue(
            ambient_url=ambient_url,
            sfx_url=sfx_url,
            ambient_prompt=ctx.ambient_prompt,
            sfx_prompt=ctx.sfx_prompt or "",
            from_cache=from_cache,
        )

    async def generate_ambient(self, prompt: str, duration_seconds: int = 20) -> str | None:
        """Generate a loopable ambient track. Returns media-proxy URL or None."""
        return await self._call_audiogen(prompt, duration_seconds, track="ambient")

    async def generate_sfx(self, prompt: str) -> str | None:
        """Generate a one-shot SFX. Returns media-proxy URL or None."""
        return await self._call_audiogen(prompt, duration_seconds=5, track="sfx")

    # ── internal helpers ──────────────────────────────────────────────────────

    async def _get_ambient(
        self, ctx: AcousticContext, location_seed: str
    ) -> tuple[str | None, bool]:
        cache_key = (
            "audiocraft:ambient:" + hashlib.sha256(location_seed.encode()).hexdigest()[:16]
            if location_seed
            else ctx.cache_key
        )
        cached = await self._redis_get(cache_key)
        if cached:
            return cached, True
        url = await self._call_audiogen(ctx.ambient_prompt, duration_seconds=20, track="ambient")
        if url:
            await self._redis_set(cache_key, url, ttl=self._ambient_ttl)
        return url, False

    async def _get_sfx(self, ctx: AcousticContext) -> str | None:
        sfx_prompt = ctx.sfx_prompt
        if not sfx_prompt:
            return None
        cache_key = ctx.sfx_cache_key
        if cache_key:
            cached = await self._redis_get(cache_key)
            if cached:
                return cached
        url = await self._call_audiogen(sfx_prompt, duration_seconds=5, track="sfx")
        if url and cache_key:
            await self._redis_set(cache_key, url, ttl=self._sfx_ttl)
        return url

    async def _call_audiogen(
        self, prompt: str, duration_seconds: int, track: str  # noqa: ARG002
    ) -> str | None:
        if not self._session:
            _log.warning("AudioCraftService not started; skipping generation")
            return None
        try:
            async with self._session.post(
                f"{self._url}/generate",
                json={"prompt": prompt, "duration": duration_seconds},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                filename = data.get("filename")
                if not filename:
                    return None
                return f"{self._media_proxy_url}/assets/audio/{filename}"
        except Exception as exc:
            _log.debug("AudioCraftService._call_audiogen failed: %s", exc)
            return None

    async def _redis_get(self, key: str) -> str | None:
        if not self._redis:
            return None
        try:
            value = await self._redis.get(key)
            return value.decode() if isinstance(value, bytes) else value
        except Exception:
            return None

    async def _redis_set(self, key: str, value: str, ttl: int) -> None:
        if not self._redis:
            return
        try:
            await self._redis.setex(key, ttl, value)
        except Exception:
            pass
