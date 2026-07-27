from __future__ import annotations

import argparse
import json

from okto_pulse.community.commands import kg_migrate_schema as command


def test_single_board_command_emits_json(monkeypatch, capsys) -> None:
    async def compose(*, list_all: bool):
        assert list_all is False
        return []

    monkeypatch.setattr(command, "_compose_and_list_boards", compose)
    monkeypatch.setattr(
        command,
        "_run_single_board",
        lambda board_id: {
            "board_id": board_id,
            "migrated": True,
            "columns_added": {},
            "errors": [],
            "duration_ms": 1,
        },
    )
    monkeypatch.setattr(
        "okto_pulse.community.serve_lock.assert_no_live_server",
        lambda *_args, **_kwargs: None,
    )

    result = command.run(
        argparse.Namespace(board_id="board-1", all_boards=False)
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["board_id"] == "board-1"


def test_all_boards_command_iterates_composed_catalog(monkeypatch, capsys) -> None:
    async def compose(*, list_all: bool):
        assert list_all is True
        return [("board-1", "One"), ("board-2", "Two")]

    monkeypatch.setattr(command, "_compose_and_list_boards", compose)
    monkeypatch.setattr(
        command,
        "_run_single_board",
        lambda board_id: {
            "board_id": board_id,
            "migrated": True,
            "columns_added": {},
            "errors": [],
            "duration_ms": 1,
        },
    )
    monkeypatch.setattr(
        "okto_pulse.community.serve_lock.assert_no_live_server",
        lambda *_args, **_kwargs: None,
    )

    result = command.run(argparse.Namespace(board_id=None, all_boards=True))

    output = capsys.readouterr().out
    assert result == 0
    assert "board-1 (One)" in output
    assert "board-2 (Two)" in output


def test_parser_requires_exactly_one_target(monkeypatch) -> None:
    captured: list[tuple[str | None, bool]] = []

    def fake_run(args) -> int:
        captured.append((args.board_id, args.all_boards))
        return 0

    monkeypatch.setattr(command, "run", fake_run)

    assert command.main(["--board", "board-1"]) == 0
    assert command.main(["--all-boards"]) == 0
    assert captured == [("board-1", False), (None, True)]
