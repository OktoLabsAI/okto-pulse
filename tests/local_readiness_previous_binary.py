"""Locally authenticated SC24 proof against a SHA-pinned predecessor wheel.

This module is selected explicitly by the Pulse scenario profile. It is not
part of the general test suite because the retained release wheel is an
operator artifact outside the repository snapshot.
"""

from pathlib import Path

from scripts.verify_readiness_previous_binary import verify
from test_readiness_schema_upgrade import _v401_predecessor


PREVIOUS_WHEEL = (Path.home() / ".okto-pulse" / "test-runners" / "artifacts" /
                  "previous-okto-pulse-0.4.2.whl")
PREVIOUS_SHA256 = "4141bf77c93ce0a2ebc266540eac158d00918f1504c2bc8a07e30eb9649b971b"


def test_previous_community_binary_refuses_upgraded_copy(tmp_path):
    assert PREVIOUS_WHEEL.is_file(), "retained previous Community wheel is required"
    predecessor = tmp_path / "pulse.sqlite3"
    _v401_predecessor(predecessor)
    result = verify(predecessor, PREVIOUS_WHEEL, PREVIOUS_SHA256)
    assert result["old_refused"] is True
    assert result["old_schema_version"] == 401
    assert result["upgraded_copy_unchanged"] is True
    assert result["predecessor_backup_verified"] is True
