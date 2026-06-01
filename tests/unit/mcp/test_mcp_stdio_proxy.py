"""Tests for the Phase 50 MCP stdio proxy.

Two surfaces:

- ``_resolve_bearer_token`` argument resolution (CLI flag, file path,
  missing/empty/unreadable)
- ``_build_parser`` shape (the public CLI contract)

The end-to-end "subprocess actually serves MCP over stdio" path is
covered by the live verify script (``scripts/verify_mcp_stdio.py``):
spawning a subprocess inside the pytest event loop in a way that
reliably handles pipe lifecycles is finicky and the live verify
already proves the wiring against the real SDK client.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from pronaos.mcp.stdio_proxy import _build_parser, _resolve_bearer_token


class TestResolveBearerToken:
    def test_inline_api_key_wins(self) -> None:
        ns = argparse.Namespace(api_key="pn_test_abc", api_key_file=None)
        assert _resolve_bearer_token(ns) == "pn_test_abc"

    def test_strips_whitespace_from_inline_key(self) -> None:
        ns = argparse.Namespace(api_key="  pn_test_abc  \n", api_key_file=None)
        assert _resolve_bearer_token(ns) == "pn_test_abc"

    def test_reads_from_file(self, tmp_path: Path) -> None:
        p = tmp_path / "key"
        p.write_text("pn_test_from_file\n", encoding="utf-8")
        ns = argparse.Namespace(api_key=None, api_key_file=str(p))
        assert _resolve_bearer_token(ns) == "pn_test_from_file"

    def test_strips_trailing_newline_from_file(self, tmp_path: Path) -> None:
        p = tmp_path / "key"
        p.write_text("pn_test_xyz\n\n   ", encoding="utf-8")
        ns = argparse.Namespace(api_key=None, api_key_file=str(p))
        assert _resolve_bearer_token(ns) == "pn_test_xyz"

    def test_missing_key_exits_with_message(self) -> None:
        ns = argparse.Namespace(api_key=None, api_key_file=None)
        with pytest.raises(SystemExit) as exc_info:
            _resolve_bearer_token(ns)
        msg = str(exc_info.value)
        assert "--api-key" in msg
        assert "--api-key-file" in msg

    def test_empty_file_exits_with_message(self, tmp_path: Path) -> None:
        p = tmp_path / "empty"
        p.write_text("\n\n  \n", encoding="utf-8")
        ns = argparse.Namespace(api_key=None, api_key_file=str(p))
        with pytest.raises(SystemExit) as exc_info:
            _resolve_bearer_token(ns)
        assert "empty" in str(exc_info.value)

    def test_missing_file_exits_with_message(self, tmp_path: Path) -> None:
        ns = argparse.Namespace(
            api_key=None, api_key_file=str(tmp_path / "doesnotexist")
        )
        with pytest.raises(SystemExit) as exc_info:
            _resolve_bearer_token(ns)
        assert "cannot read" in str(exc_info.value)


class TestParser:
    def test_default_gateway_url(self) -> None:
        ns = _build_parser().parse_args(["--api-key", "x"])
        assert ns.gateway_url == "http://127.0.0.1:8080"

    def test_gateway_url_override(self) -> None:
        ns = _build_parser().parse_args(
            ["--gateway-url", "https://gw.example.com", "--api-key", "x"]
        )
        assert ns.gateway_url == "https://gw.example.com"

    def test_api_key_and_file_mutually_exclusive(self) -> None:
        # argparse exits 2 on conflicting mutually-exclusive args.
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(
                ["--api-key", "x", "--api-key-file", "/tmp/y"]
            )
        assert exc_info.value.code == 2
