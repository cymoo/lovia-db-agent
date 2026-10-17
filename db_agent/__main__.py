"""``python -m db_agent`` / ``lovia-db-agent``: serve the agent, or check its configuration."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import find_dotenv, load_dotenv
from lovia import UserError
from lovia.web import generate_token, is_loopback, serve

from .app import build_app
from .check import check
from .config import load_settings


def main(argv: list[str] | None = None) -> None:
    # Before the flags: their defaults may come from .env. The .env of the
    # directory it runs in (or above), not of wherever the package is installed.
    load_dotenv(find_dotenv(usecwd=True))
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in (*_COMMANDS, "-h", "--help"):
        argv = ["serve", *argv]  # the command is optional
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        if args.command == "check":
            sys.exit(0 if asyncio.run(check(settings)) else 1)
        token = os.environ.get("LOVIA_WEB_TOKEN") or None
        if token is None and not is_loopback(args.host):
            token = generate_token()  # serve() prints the link that carries it
        serve(build_app(settings, token=token), host=args.host, port=args.port)
    except UserError as exc:
        sys.exit(f"error: {exc}")


_COMMANDS = ("serve", "check")


def _parser() -> argparse.ArgumentParser:
    config = argparse.ArgumentParser(add_help=False)
    config.add_argument(
        "--config",
        default=os.environ.get("DB_AGENT_CONFIG", "config.yaml"),
        help="the YAML configuration (default: $DB_AGENT_CONFIG or config.yaml)",
    )
    parser = argparse.ArgumentParser(
        prog="lovia-db-agent",
        description="Ask PostgreSQL / MySQL in plain language. Without a command: serve.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve_cmd = sub.add_parser(
        "serve", parents=[config], help="serve the chat UI (the default command)"
    )
    serve_cmd.add_argument(
        "--host",
        default=os.environ.get("DB_AGENT_HOST", "127.0.0.1"),
        help="default: $DB_AGENT_HOST or 127.0.0.1; beyond it, a login token is required",
    )
    serve_cmd.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DB_AGENT_PORT", "8000")),
        help="default: $DB_AGENT_PORT or 8000",
    )
    sub.add_parser(
        "check",
        parents=[config],
        help="connect to every database and report what the agent would see",
    )
    return parser


if __name__ == "__main__":
    main()
