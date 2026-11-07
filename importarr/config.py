import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    lidarr_url: str
    lidarr_api_key: str
    # LLM (OpenAI-compatible endpoint)
    llm_base_url: str | None = None
    llm_api_key: str = ""
    llm_model: str | None = None
    llm_max_rounds: int = 15
    llm_transport: object | None = None  # test hook: httpx transport for the LLM client
    # Notifications (Apprise)
    apprise_url: str | None = None
    # Behavior
    dry_run: bool = True
    retry_backoffs: tuple[int, ...] = (30, 120, 600)
    # File access allowlist for tag inspection (comma-separated roots)
    audio_paths: tuple[str, ...] = ()
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        def _b(key: str, default: bool) -> bool:
            raw = os.environ.get(key)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        backoffs_raw = os.environ.get("RETRY_BACKOFFS", "30,120,600")
        backoffs = tuple(
            int(x) for x in backoffs_raw.split(",") if x.strip().isdigit()
        ) or (30, 120, 600)
        return cls(
            lidarr_url=os.environ["LIDARR_URL"].rstrip("/"),
            lidarr_api_key=os.environ["LIDARR_API_KEY"],
            llm_base_url=os.environ.get("LLM_BASE_URL") or None,
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_model=os.environ.get("LLM_MODEL") or None,
            llm_max_rounds=int(os.environ.get("LLM_MAX_ROUNDS", "15")),
            dry_run=_b("DRY_RUN", True),
            apprise_url=os.environ.get("APPRISE_URL") or None,
            audio_paths=tuple(
                os.path.realpath(p.strip())
                for p in os.environ.get("AUDIO_PATHS", "").split(",")
                if p.strip()),
            retry_backoffs=backoffs,
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
