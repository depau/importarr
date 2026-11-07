"""Entry point: python -m importarr."""

import logging
import os

import uvicorn

from .app import app as build_app, setup_logging
from .config import Config

log = logging.getLogger("importarr")


def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    log.info(
        "importarr starting (dry_run=%s, llm=%s, apprise=%s)",
        cfg.dry_run, cfg.llm_model or "not configured", "yes" if cfg.apprise_url else "no",
    )
    app = build_app(cfg)

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8323")))


if __name__ == "__main__":
    main()
