"""Run a loopback-only, no-key service. See README.md before deployment."""

import argparse
from pathlib import Path

from aiohttp import web

from .app import create_app

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--directory", required=True, type=Path)
parser.add_argument("--port", type=int, default=8080)
parser.add_argument(
    "--live", action="store_true", help="Use OPENAI_API_KEY and the default native model"
)
args = parser.parse_args()
web.run_app(
    create_app(args.directory, live=args.live), host="127.0.0.1", port=args.port, access_log=None
)
