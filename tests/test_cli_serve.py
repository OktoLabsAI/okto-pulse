"""Unit tests for the `okto-pulse serve` CLI command.

Tests argparse wiring and the pure `is_port_in_use` helper via
subprocess and direct function calls.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

REPO_SRC = Path(__file__).parent.parent / "src"
CORE_SRC = Path(__file__).parent.parent.parent / "okto-pulse-core" / "src"

for p in (str(REPO_SRC), str(CORE_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

for mod in list(sys.modules):
    if mod.startswith("okto_pulse.community"):
        del sys.modules[mod]


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def test_serve_subparser_has_port_flags():
    """The serve subparser exposes --api-port and --mcp-port."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'{}'); "
         "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
         "serve", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "--api-port" in result.stdout
    assert "--mcp-port" in result.stdout


# ---------------------------------------------------------------------------
# is_port_in_use — pure function tests
# ---------------------------------------------------------------------------


def test_is_port_in_use_returns_false_for_unused_port():
    """_is_port_in_use returns False for a port that nothing is listening on."""
    from okto_pulse.community.cli import _is_port_in_use
    # Port 0 lets the OS pick a free port; we bind and immediately close.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]
    # Port should now be free
    assert _is_port_in_use(port) is False


def test_is_port_in_use_returns_true_when_bound(monkeypatch):
    """_is_port_in_use returns True when something is listening on the port."""
    from okto_pulse.community import cli as cli_mod

    # Patch socket.socket to always return 0 (connection successful)
    class FakeSocket:
        def __init__(self, *a, **k):
            pass
        def connect_ex(self, *a):
            return 0
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    monkeypatch.setattr(cli_mod.socket, "socket", FakeSocket)
    assert cli_mod._is_port_in_use(12345) is True
