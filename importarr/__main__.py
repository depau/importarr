"""Entry point: python -m importarr.

`importarr` (or `serve`) runs the webhook server (default, what Docker runs).
`importarr import <path>...` imports an existing tagged audio folder via the
Lidarr manual-import engine + LLM agent.
"""
import asyncio
import logging
import os
import sys

import uvicorn

from .app import app as build_app, setup_logging
from .config import Config
from .lidarr import LidarrClient
from .notify import Notifier

log = logging.getLogger("importarr")


def _serve(cfg: Config) -> None:
    log.info(
        "importarr starting (dry_run=%s, llm=%s, apprise=%s)",
        cfg.dry_run, cfg.llm_model or "not configured", "yes" if cfg.apprise_url else "no",
    )
    application = build_app(cfg)
    uvicorn.run(application, host="0.0.0.0", port=int(os.environ.get("PORT", "8323")))


def _import_paths(cfg: Config, paths: list[str]) -> None:
    from types import SimpleNamespace

    from . import takeout

    async def _run():
        lidarr = LidarrClient(cfg.lidarr_url, cfg.lidarr_api_key)
        app = SimpleNamespace(state=SimpleNamespace(
            cfg=cfg,
            lidarr=lidarr,
            notifier=Notifier(cfg.apprise_url),
        ))
        try:
            result = await takeout.import_paths(app, paths)
        finally:
            await lidarr.close()
        # plain stdout summary for docker exec
        for r in result["reports"]:
            print(r["path"])
            if cfg.dry_run:
                if r.get("would_commit"):
                    print(f"  would commit: {', '.join(r['would_commit'])}")
            else:
                if r.get("committed_names"):
                    print(f"  submitted: {', '.join(r['committed_names'])}")
            for key in ("agent", "env", "unresolved"):
                if r.get(key):
                    print(f"  {key}: {', '.join(r[key])}")
            for s in r.get("skipped", []):
                print(f"  skip: {s}")
            if r.get("error"):
                print(f"  error: {r['error']}")
            if cfg.dry_run:
                print("  (dry run: nothing was changed; re-run with DRY_RUN=false to commit)")

    asyncio.run(_run())


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("serve", "webhook"):
        cmd = []
    elif args and args[0] == "import":
        cmd = args[1:]
    else:
        cmd = None

    if cmd is None:
        cfg = Config.from_env()
        setup_logging(cfg.log_level)
        _serve(cfg)
        return

    if not cmd:
        print("usage: importarr import <path> [path...]", file=sys.stderr)
        print("       importarr [serve]   (run the webhook server)", file=sys.stderr)
        sys.exit(2)

    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    _import_paths(cfg, cmd)


if __name__ == "__main__":
    main()
