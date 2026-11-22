from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .config import get_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "huggingface_hub", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = get_settings()
    print(f"\n  Ouy 已启动：http://{settings.host}:{settings.port}\n", flush=True)
    uvicorn.run(create_app(), host=settings.bind or settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
