"""Scenario entry points for the committed Card status frontend snapshot.

The local runner supplies a disposable checkout. JavaScript dependencies come
from the locally installed toolchain; no Pulse runtime or board data is used.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import unittest


_DEPENDENCIES = Path(r"D:\Projetos\Techridy\okto-pulse\frontend\node_modules")


class CardStatusFrontendScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.frontend = cls.root / "frontend"
        cls.link = cls.frontend / "node_modules"
        if not (_DEPENDENCIES / "vitest" / "vitest.mjs").is_file():
            raise AssertionError("local Vitest toolchain unavailable")
        if cls.link.exists():
            raise AssertionError("snapshot unexpectedly contains node_modules")
        # Junction creation needs no administrator privilege on Windows. The
        # snapshot remains isolated; only packages are shared read-only.
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(cls.link), str(_DEPENDENCIES)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0 or not cls.link.is_dir():
            raise AssertionError("could not mount local JavaScript toolchain")

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "link", None) and cls.link.is_dir():
            os.rmdir(cls.link)

    def _run(self, package: str, entry: str, *args: str) -> None:
        result = subprocess.run(
            ["node", str(_DEPENDENCIES / package / entry), *args],
            cwd=self.frontend,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=105,
        )
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-3000:])
        if package == "vitest":
            self.assertRegex(result.stdout, re.compile(r"Tests\s+\d+ passed"))

    def test_sc16_kanban_count_and_creation(self) -> None:
        self._run(
            "vitest", "vitest.mjs", "run",
            "src/components/kanban/__tests__/KanbanColumn.counts.test.tsx",
            "src/components/kanban/__tests__/CreateCardModal.knowledgePropagation.test.tsx",
            "src/components/kanban/__tests__/CardModal.test.tsx",
            "-t", "counts|creation|status|lifecycle|starts|create|current and canonically allowed",
        )

    def test_sc17_rejected_transition_and_refresh(self) -> None:
        self._run(
            "vitest", "vitest.mjs", "run",
            "src/components/kanban/__tests__/KanbanBoard.refresh.test.tsx",
            "src/components/kanban/__tests__/kanbanDnd.test.ts",
            "src/components/kanban/__tests__/CardModal.test.tsx",
            "-t", "refresh|drop|transition|policy|destination|authorization|lifecycle",
        )
        board = (self.frontend / "src/components/kanban/KanbanBoard.tsx").read_text(encoding="utf-8")
        self.assertIn("optimisticMoveCard(cardId, fromStatus, originalIndex)", board)
        self.assertIn("toast.error(err instanceof Error ? err.message", board)
        self.assertIn("refreshColumns();", board)

    def test_sc18_types_docs_and_history(self) -> None:
        self._run("typescript", "bin/tsc", "-b", "--pretty", "false")
        self._run(
            "vitest", "vitest.mjs", "run",
            "src/components/shared/__tests__/ActivityLogList.test.tsx",
        )
        types = (self.frontend / "src/types/index.ts").read_text(encoding="utf-8")
        doc = (self.root / "docs/CARD_STATUS_LIFECYCLE.md").read_text(encoding="utf-8")
        self.assertIn("export const CREATABLE_CARD_STATUSES: CardStatus[] = [\n  'not_started'", types)
        self.assertNotIn("  | 'started'", types.split("// Card priority", 1)[0])
        self.assertIn("Historical", doc)
        self.assertIn("REST and MCP", doc)
