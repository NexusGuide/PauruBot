"""`paurubot restore` reads every option it declares (regression: --print-master-key was missing)."""

from __future__ import annotations

from paurubot.cli.main import build_parser


def test_restore_accepts_print_master_key() -> None:
    args = build_parser().parse_args(["restore", "backup.pbak", "--print-master-key"])
    assert args.print_master_key is True


def test_restore_defaults_do_not_print_the_key() -> None:
    args = build_parser().parse_args(["restore", "backup.pbak"])
    assert args.print_master_key is False
    assert args.yes is False
    assert args.write_env is None
