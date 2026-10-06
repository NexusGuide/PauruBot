"""Operator command line (ARC-05): `paurubot <command>`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any

from paurubot import __version__
from paurubot.config import get_settings
from paurubot.logs import setup_logging

Command = Callable[[argparse.Namespace], Coroutine[Any, Any, int]]
Configure = Callable[[argparse.ArgumentParser], object]
COMMANDS: dict[str, tuple[Command, str, Configure | None]] = {}


def command(name: str, help_text: str, configure: Configure | None = None) -> Callable[[Command], Command]:
    def register(func: Command) -> Command:
        COMMANDS[name] = (func, help_text, configure)
        return func

    return register


def build_parser() -> argparse.ArgumentParser:
    from paurubot.cli import commands  # noqa: F401  (registers commands)

    parser = argparse.ArgumentParser(prog="paurubot", description="PauruBot operator commands")
    parser.add_argument("--version", action="version", version=f"PauruBot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text, configure) in sorted(COMMANDS.items()):
        child = sub.add_parser(name, help=help_text)
        if configure:
            configure(child)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    if args.command in {"api", "bot", "worker"}:
        from paurubot.logs import setup_error_monitoring

        setup_error_monitoring(args.command)
    func = COMMANDS[args.command][0]
    try:
        return asyncio.run(func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
