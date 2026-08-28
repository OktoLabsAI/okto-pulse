"""The shared Cypher policy, and the proof that no engine answers differently.

The point of the module is that there is exactly one answer to "is this a
write". These tests therefore spend most of their effort on the *agreement*
between the three adapters that used to answer it themselves, and on the cases
where a fail-closed classifier is easiest to get wrong: text hidden in comments
and literals, a second statement smuggled after a semicolon, and procedures.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from repo_layout import resolve_core_repo

REPO_SRC = Path(__file__).parent.parent / "src"
CORE_SRC = resolve_core_repo(REPO_SRC.parent) / "src"

for _path in (str(REPO_SRC), str(CORE_SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from okto_pulse.community.adapters.cypher_statement_policy import (
    PROVEN_READ_ONLY_CALLS,
    leading_statement_token,
    statement_is_write,
    statement_kind,
    statement_uses_vector,
    strip_comments_and_literals,
)

# Every case is (statement, is_write). Shared by the policy tests and by the
# agreement test, so a new case is automatically required of all three
# adapters rather than only of the helper.
POLICY_CASES: tuple[tuple[str, bool], ...] = (
    # plain reads
    ("MATCH (n:Decision) RETURN n", False),
    ("MATCH (n) RETURN n;", False),
    ("MATCH (n) WHERE n.title = 'CREATE (x)' RETURN n", False),
    ("MATCH (n) RETURN n // CREATE (m:Decision)", False),
    ("/* CREATE (m) */ MATCH (n) RETURN n", False),
    # the deliberately small read-only procedure allowlist
    ("CALL SHOW_TABLES()", False),
    ("CALL SHOW_INDEXES()", False),
    ("CALL SHOW_CONNECTION('x')", False),
    ("CALL TABLE_INFO('Decision')", False),
    ("CALL QUERY_VECTOR_INDEX('Decision', 'idx', $vector, 8)", False),
    # writes
    ("CREATE (n:Decision)", True),
    ("MATCH (n) SET n.value = 1", True),
    ("MATCH (n) DETACH DELETE n", True),
    ("UNWIND [1] AS i MERGE (n:Decision {id: i})", True),
    ("MATCH (n) WITH n SET n.x = 1", True),
    ("EXPLAIN CREATE (n:Decision)", True),
    ("ATTACH 'other' AS other (dbtype kuzu)", True),
    ("INSTALL vector", True),
    ("LOAD EXTENSION vector", True),
    ("CHECKPOINT", True),
    ("COPY Decision FROM 'rows.csv'", True),
    ("EXPORT DATABASE 'dump'", True),
    ("VACUUM", True),
    # fail-closed: a second statement, however it is hidden
    ("MATCH (n) RETURN n; CREATE (m:Decision)", True),
    ("MATCH (n) RETURN n ; DROP TABLE Decision", True),
    # fail-closed: procedures outside the allowlist
    ("CALL db.awaitIndexes()", True),
    ("CALL CREATE_VECTOR_INDEX('Decision', 'idx', 'embedding')", True),
    ("CALL DROP_VECTOR_INDEX('Decision', 'idx')", True),
    ("CALL { CALL some_proc() } RETURN 1", True),
    ("CALL SHOW_TABLES() CALL SHOW_INDEXES()", True),
    # fail-closed: nothing to classify
    ("", True),
    ("   ", True),
    ("// only a comment", True),
)


@pytest.mark.parametrize(("statement", "expected"), POLICY_CASES)
def test_the_policy_classifies_each_statement(statement: str, expected: bool) -> None:
    assert statement_is_write(statement) is expected, statement


class TestNothingSlipsPastTheFence:
    """The cases a leading-token classifier gets wrong."""

    def test_a_comment_cannot_smuggle_a_second_statement(self) -> None:
        # The separator is inside the comment, so what remains is one read.
        assert statement_is_write("MATCH (n) RETURN n /* ; CREATE (m) */") is False
        # ...but a real separator outside a comment is still a write.
        assert statement_is_write("MATCH (n) RETURN n /* c */ ; CREATE (m)") is True

    def test_a_literal_is_never_read_as_grammar(self) -> None:
        assert strip_comments_and_literals("RETURN 'CREATE (x)'").strip() == "RETURN"
        assert statement_is_write("MATCH (n) WHERE n.t = 'DROP TABLE x' RETURN n") is (
            False
        )

    def test_an_unknown_procedure_is_a_write_even_beside_an_allowlisted_one(
        self,
    ) -> None:
        assert statement_is_write("CALL SHOW_TABLES() CALL db.awaitIndexes()") is True

    def test_a_nested_call_is_a_write_even_when_the_inner_name_is_allowlisted(
        self,
    ) -> None:
        assert statement_is_write("CALL { CALL SHOW_TABLES() } RETURN 1") is True

    def test_only_the_allowlist_may_be_read(self) -> None:
        for name in PROVEN_READ_ONLY_CALLS:
            assert statement_is_write(f"CALL {name}()") is False, name
        # A name that merely starts with an allowlisted one is not allowlisted.
        assert statement_is_write("CALL SHOW_TABLES_AND_DROP()") is True


class TestTheTwoTelemetryVocabulariesStayDistinct:
    """Two published contracts, deliberately not merged into one."""

    def test_the_detailed_vocabulary_is_unchanged(self) -> None:
        assert statement_kind("MATCH (n) RETURN n") == "MATCH_READ"
        assert statement_kind("MATCH (n) SET n.value = $value") == "MATCH_SET"
        assert statement_kind("PROFILE MATCH (n) DELETE n") == "MATCH_DELETE"
        assert statement_kind("CREATE (n:Decision)") == "CREATE"
        assert statement_kind("CALL SHOW_TABLES() RETURN name") == "CALL"
        assert statement_kind("unrecognized private payload") == "OTHER"
        assert (
            statement_kind("CALL CREATE_VECTOR_INDEX('t','i','e')")
            == "CALL_CREATE_VECTOR_INDEX"
        )

    def test_the_coarse_vocabulary_is_unchanged(self) -> None:
        assert leading_statement_token("MATCH (d) RETURN d") == "MATCH"
        assert leading_statement_token("  create (:X)") == "CREATE"
        assert leading_statement_token("EXPLAIN MATCH (n) RETURN n") == "MATCH"

    def test_neither_vocabulary_ever_echoes_the_statement(self) -> None:
        secret = "MATCH (n) WHERE n.token = 'super-secret-value' RETURN n"
        assert "secret" not in statement_kind(secret).lower()
        assert "secret" not in leading_statement_token(secret).lower()


class TestVectorDetection:
    def test_vector_use_is_detected_past_comments_and_literals(self) -> None:
        assert statement_uses_vector("CALL QUERY_VECTOR_INDEX('t','i',$v,5)") is True
        assert statement_uses_vector("MATCH (n) RETURN n.embedding") is True
        # Mentioning the word in a literal or comment is not using the index.
        assert statement_uses_vector("MATCH (n) WHERE n.t = 'embedding' RETURN n") is (
            False
        )
        assert statement_uses_vector("MATCH (n) RETURN n // EMBEDDING") is False

    def test_a_plain_read_uses_no_vector(self) -> None:
        assert statement_uses_vector("MATCH (n:Decision) RETURN n") is False


# --- the actual point: one authority ------------------------------------------------------------


def _adapter_classifiers():
    """The three functions that used to each own a copy of this policy."""

    from okto_pulse.community.adapters.grafx_cypher_executor import (
        statement_is_write as grafx_executor,
    )
    from okto_pulse.community.adapters.grafx_global_discovery_runtime import (
        _statement_is_write as grafx_global,
    )
    from okto_pulse.community.adapters.kuzu_graph_transaction import (
        _statement_is_write as ladybug,
    )

    return {
        "ladybug_transaction": ladybug,
        "grafx_executor": grafx_executor,
        "grafx_global_discovery": grafx_global,
    }


@pytest.mark.parametrize(
    ("statement", "expected"),
    [case for case in POLICY_CASES if case[0].strip()],
)
def test_every_adapter_returns_the_shared_answer(
    statement: str, expected: bool
) -> None:
    """No engine may disagree with the policy, in either direction.

    This is the regression that matters: before the extraction the Grafx side
    fenced the read-only allowlist that the Ladybug side accepted, and nothing
    in the suite noticed.
    """

    answers = {
        name: classifier(statement)
        for name, classifier in _adapter_classifiers().items()
    }

    assert set(answers.values()) == {expected}, (statement, answers)


def test_the_grafx_global_runtime_still_refuses_unusable_input() -> None:
    # Its own contract, kept: the shared policy decides what a write is, not
    # whether the caller passed something usable at all.
    from okto_pulse.community.adapters.grafx_global_discovery_runtime import (
        _statement_is_write as grafx_global,
    )

    for bad in ("", "   ", None, 7):
        with pytest.raises(ValueError):
            grafx_global(bad)  # type: ignore[arg-type]


def test_converging_the_allowlist_removes_only_an_unnecessary_fence() -> None:
    """Grafx stops fencing the allowlist -- and nothing else.

    The Grafx transaction fences on ``statement_is_write``.  Converging means
    an allowlisted introspection/vector read no longer takes a writer fence it
    never needed; it must NOT mean an unknown or nested procedure slips through.
    """

    from okto_pulse.community.adapters.grafx_cypher_executor import statement_is_write

    # No longer fenced.
    assert statement_is_write("CALL QUERY_VECTOR_INDEX('t','i',$v,5)") is False
    assert statement_is_write("CALL SHOW_TABLES()") is False
    # Still fenced, and this is the half that must never regress.
    assert statement_is_write("CALL db.awaitIndexes()") is True
    assert statement_is_write("CALL { CALL SHOW_TABLES() } RETURN 1") is True
    assert statement_is_write("CALL SHOW_TABLES(); CREATE (n)") is True


def test_a_vector_read_stays_a_writer_operation_on_the_ladybug_global_path() -> None:
    """``LOAD VECTOR`` writes Ladybug's WAL, so the allowlist must not free it.

    ``search_decision_digests`` issues ``CALL QUERY_VECTOR_INDEX``.  The shared
    policy now calls that a read, which is correct for the statement itself --
    but the Ladybug Global runtime still has to take the writer token, because
    loading the extension mutates. This is the interaction the convergence
    could plausibly have broken.
    """

    from okto_pulse.community.adapters.global_discovery_runtime import (
        _statement_requires_vector_extension,
    )

    vector_read = "CALL QUERY_VECTOR_INDEX('Decision', 'idx', $vector, 8)"

    assert statement_is_write(vector_read) is False
    assert _statement_requires_vector_extension(vector_read) is True
    # A read that touches no vector still costs nothing.
    assert _statement_requires_vector_extension("MATCH (n) RETURN n") is False


def test_the_ladybug_transaction_still_exports_its_historic_names() -> None:
    from okto_pulse.community.adapters import kuzu_graph_transaction as ladybug

    assert ladybug._statement_is_write is statement_is_write
    assert ladybug._statement_kind is statement_kind
    assert ladybug._PROVEN_READ_ONLY_CALLS == PROVEN_READ_ONLY_CALLS
    assert "_statement_is_write" in ladybug.__all__
    assert "_statement_kind" in ladybug.__all__


def test_the_policy_module_does_not_load_an_engine() -> None:
    """The helper runs on the Grafx path, so importing it must not pull Ladybug.

    Checked in a fresh interpreter: an import that only happens transitively
    would still be invisible to an in-process assertion made after the whole
    test suite has already imported everything.
    """

    script = (
        "import sys\n"
        "import okto_pulse.community.adapters.cypher_statement_policy as policy\n"
        "assert policy is not None\n"
        "leaked = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name == 'ladybug' or name.startswith('ladybug.')\n"
        "    or name == 'kuzu' or name.startswith('kuzu.')\n"
        ")\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={
            **__import__("os").environ,
            "PYTHONPATH": __import__("os").pathsep.join((str(REPO_SRC), str(CORE_SRC))),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LEAKED:", result.stdout
