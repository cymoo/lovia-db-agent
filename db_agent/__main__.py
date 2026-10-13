"""``python -m db_agent`` / ``lovia-db-agent``: serve the agent."""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from lovia.web import serve

from .app import build_app
from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="lovia-db-agent", description=__doc__)
    parser.add_argument("--config", default=os.environ.get("DB_AGENT_CONFIG", "config.yaml"))
    parser.add_argument("--host", default=os.environ.get("DB_AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DB_AGENT_PORT", "8000")))
    args = parser.parse_args()
    load_dotenv()
    serve(build_app(load_settings(args.config)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
