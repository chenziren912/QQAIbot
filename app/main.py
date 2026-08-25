"""Entrypoint for the local QQ group AI agent."""

from __future__ import annotations

import argparse
import logging
from typing import Optional, Sequence

import uvicorn

from .web import create_app


app = create_app()


def run(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run the loopback-only QQ group AI agent.")
    parser.add_argument("--host", default="127.0.0.1", help="Must remain 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        parser.error("For safety, --host must be 127.0.0.1.")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    # Keep the package-level application as the sole owner of its SQLite
    # connection.  The PowerShell launcher imports this same object too.
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":  # pragma: no cover
    run()
