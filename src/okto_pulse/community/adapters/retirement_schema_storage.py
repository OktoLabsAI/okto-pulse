"""Physical SQLite retirement inside the offline coordinator's transaction.

This mechanism neither authorizes archival nor admits startup. The caller must
verify retained archive/data/permission/graph receipts under its writer fences,
disable foreign keys only on its reserved connection, and restore that setting
after commit or rollback. Rebuilding a referenced table with FK enforcement on
would execute ON DELETE CASCADE even when those constraints are deferred.
"""

import hashlib
import re

from .sprint_retirement_archive import _cell, _encode, _quoted

RETIRED_TABLES = ("sprint_activation_baselines", "sprint_history", "sprint_qa_items", "sprints")
_TEMP = "cards_retirement_candidate"
_LIMIT = 64 * 1024 * 1024
_ROWS = 100_000
_LEX = re.compile(r"\s+|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|"
    r"`(?:[^`]|``)*`|\[[^\]]*\]|[\w]+|[^\s]", re.UNICODE)


def _tokens(sql):
    result = []
    for match in _LEX.finditer(sql):
        raw = match.group()
        if raw.isspace() or raw.startswith(("--", "/*")):
            continue
        value = raw
        if raw[0] in "'\"`[":
            closing = "]" if raw[0] == "[" else raw[0]
            if len(raw) < 2 or raw[-1] != closing:
                raise ValueError("retirement_schema_sql_unclassified")
            value = raw[1:-1].replace(closing * 2, closing)
        result.append((value.casefold(), match.start(), match.end()))
    return result


def _rewrite_cards(sql):
    """Remove only the known nullable origin column and its single-column FK.

    Keep original SQL for every surviving declaration. Reflection/recompilation
    can normalize away collations, checks, defaults or extension expressions.
    """
    tokens = _tokens(sql)
    values = [item[0] for item in tokens]
    if values[:4] != ["create", "table", "cards", "("]:
        raise ValueError("retirement_schema_cards_ddl_unclassified")
    depth, start, pieces = 1, tokens[3][2], []
    for value, left, right in tokens[4:]:
        punctuation = sql[left:right]
        if punctuation == "(":
            depth += 1
        elif punctuation == ")":
            depth -= 1
        if depth == 1 and punctuation == "," or depth == 0:
            pieces.append(sql[start:left])
            start = right
        if depth == 0:
            break
    if depth != 0 or sql[start:].strip().strip(";").strip():
        raise ValueError("retirement_schema_cards_ddl_unclassified")
    foreign = ["references", "sprints", "(", "id", ")", "on", "delete", "set", "null"]
    column, foreign_count, kept = 0, 0, []
    for piece in pieces:
        names = [item[0] for item in _tokens(piece)]
        if names and names[0] == "sprint_id":
            if names not in (["sprint_id", "varchar", "(", "36", ")"],
                    ["sprint_id", "varchar", "(", "36", ")", *foreign]):
                raise ValueError("retirement_schema_origin_column_unclassified")
            column += 1
            foreign_count += int("references" in names)
            continue
        constraint = names[2:] if names[:1] == ["constraint"] else names
        if constraint == ["foreign", "key", "(", "sprint_id", ")", *foreign]:
            foreign_count += 1
            continue
        if set(names) & {*RETIRED_TABLES, "sprint_id"}:
            raise ValueError("retirement_schema_cards_dependency_unclassified")
        kept.append(piece)
    if column != 1 or foreign_count != 1:
        raise ValueError("retirement_schema_origin_contract_mismatch")
    return f'CREATE TABLE "{_TEMP}" (' + ",".join(kept) + ")"


def _objects(connection):
    rows = connection.exec_driver_sql("SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name").all()
    if len(rows) > 10_000 or sum(len(row[3] or "") for row in rows) > _LIMIT:
        raise ValueError("retirement_schema_inventory_limit")
    return tuple(tuple(row) for row in rows)


def _rows(connection, table, *, omit_origin=False, rowid=False):
    columns = connection.exec_driver_sql(f"PRAGMA table_xinfo({_quoted(table)})").all()
    names = [row[1] for row in columns if not (omit_origin and row[1] == "sprint_id")]
    if not names or rowid and {name.casefold() for name in names} & {"rowid", "_rowid_", "oid"}:
        raise ValueError("retirement_schema_columns_unclassified")
    selected = (["rowid"] if rowid else []) + names
    size = "+".join(f"coalesce(length(CAST({_quoted(name)} AS BLOB)),0)" for name in selected)
    count, total = connection.exec_driver_sql(f"SELECT count(*),coalesce(sum({size}),0) FROM {_quoted(table)}").one()
    if count > _ROWS or total > _LIMIT:
        raise ValueError("retirement_schema_data_limit")
    encoded = []
    used = 0
    for row in connection.exec_driver_sql(f"SELECT {','.join(map(_quoted, selected))} FROM {_quoted(table)}"):
        value = _encode([_cell(cell) for cell in row])
        used += len(value)
        if used > _LIMIT:
            raise ValueError("retirement_schema_data_limit")
        encoded.append(value)
    # Bag comparison preserves duplicates, raw JSON spacing and SQLite types.
    digest = hashlib.sha256()
    for value in sorted(encoded):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return {"columns": selected, "count": count, "bytes": used, "sha256": digest.hexdigest()}


def surviving_data(connection):
    tables = connection.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").scalars().all()
    result, count, size = {}, 0, 0
    for table in tables:
        if table in RETIRED_TABLES or table == "retirement_data_checkpoints":
            continue
        value = _rows(connection, table, omit_origin=table == "cards", rowid=table == "cards")
        count += value["count"]
        size += value["bytes"]
        if count > _ROWS or size > _LIMIT:
            raise ValueError("retirement_schema_data_limit")
        result[table] = value
    return result


def require_archived_owned_rows(connection, documents):
    """Compare all retiring cells to the already verified original archives."""
    for table in RETIRED_TABLES:
        names = [row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({_quoted(table)})")]
        if not names:
            raise ValueError("retirement_schema_source_missing")
        expected = []
        for document in documents.values():
            section = document["tables"][table]
            if names != [column["name"] for column in section["columns"]]:
                raise ValueError("retirement_schema_archive_columns_mismatch")
            expected.extend(_encode(row) for row in section["rows"])
        # Bound SQL values before transferring them into Python.
        _rows(connection, table)
        actual = [_encode([_cell(cell) for cell in row])
            for row in connection.exec_driver_sql(f"SELECT * FROM {_quoted(table)}")]
        if sorted(actual) != sorted(expected):
            raise ValueError("retirement_schema_archive_rows_mismatch")


def cut_retired_schema(connection, documents):
    """Atomic DDL primitive; caller owns receipts, transaction and FK restoration."""
    if (connection.dialect.name != "sqlite" or not connection.in_transaction()
            or connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0):
        raise ValueError("retirement_schema_reserved_connection_required")
    objects = _objects(connection)
    tables = {name for kind, name, _, _ in objects if kind == "table"}
    if not set(RETIRED_TABLES) <= tables or _TEMP in tables or "cards" not in tables:
        raise ValueError("retirement_schema_source_mismatch")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise ValueError("retirement_schema_foreign_keys_invalid")
    require_archived_owned_rows(connection, documents)
    if connection.exec_driver_sql("SELECT 1 FROM cards WHERE sprint_id IS NOT NULL LIMIT 1").first():
        raise ValueError("retirement_schema_card_origins_not_preserved")
    original = next(sql for kind, name, _, sql in objects if kind == "table" and name == "cards")
    candidate = _rewrite_cards(original)
    recreate, removed = [], {("table", name) for name in RETIRED_TABLES}
    for kind, name, owner, sql in objects:
        if owner in RETIRED_TABLES:
            removed.add((kind, name))
            continue
        if (kind, name) == ("table", "cards"):
            continue
        names = {item[0] for item in _tokens(sql or "")}
        if names & {*RETIRED_TABLES, "sprint_id"}:
            # Only the known plain origin index can retire with its column.
            index = connection.exec_driver_sql('PRAGMA index_list("cards")').mappings().all()
            match = next((row for row in index if row["name"] == name), None)
            columns = connection.exec_driver_sql(f"PRAGMA index_info({_quoted(name)})").all() if kind == "index" else []
            if not (kind == "index" and owner == "cards" and name == "ix_cards_sprint_id"
                    and match is not None and not match["unique"] and not match["partial"]
                    and [row[2] for row in columns] == ["sprint_id"]):
                raise ValueError(f"retirement_schema_dependency_unclassified:{name}")
            removed.add((kind, name))
        elif owner == "cards" and kind in {"index", "trigger"} and sql:
            recreate.append(sql)
    before = surviving_data(connection)
    columns = connection.exec_driver_sql('PRAGMA table_xinfo("cards")').all()
    writable = ["rowid", *(row[1] for row in columns if row[1] != "sprint_id" and row[6] == 0)]
    selected = ",".join(map(_quoted, writable))
    connection.exec_driver_sql(candidate)
    connection.exec_driver_sql(f"INSERT INTO {_quoted(_TEMP)} ({selected}) SELECT {selected} FROM cards")
    connection.exec_driver_sql("DROP TABLE cards")
    connection.exec_driver_sql(f"ALTER TABLE {_quoted(_TEMP)} RENAME TO cards")
    for statement in recreate:
        connection.exec_driver_sql(statement)
    for table in RETIRED_TABLES:
        connection.exec_driver_sql(f"DROP TABLE {_quoted(table)}")
    after = _objects(connection)
    unchanged = tuple(row for row in objects if row[:2] not in removed and row[:2] != ("table", "cards"))
    if tuple(row for row in after if row[:2] != ("table", "cards")) != unchanged or surviving_data(connection) != before:
        raise ValueError("retirement_schema_surviving_state_changed")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise ValueError("retirement_schema_foreign_keys_invalid")
    for kind, name, _, _ in after:
        if kind == "view":
            connection.exec_driver_sql(f"SELECT * FROM {_quoted(name)} LIMIT 0")
    return {"before_schema_sha256": hashlib.sha256(_encode(objects)).hexdigest(),
        "after_schema_sha256": hashlib.sha256(_encode(after)).hexdigest(),
        "data_sha256": hashlib.sha256(_encode(before)).hexdigest(), "cards": before["cards"]["count"]}


def require_cut_schema(connection, receipt):
    objects = _objects(connection)
    data = surviving_data(connection)
    if (any(kind == "table" and (name in RETIRED_TABLES or name == _TEMP) for kind, name, _, _ in objects)
            or any(row[1] == "sprint_id" for row in connection.exec_driver_sql('PRAGMA table_xinfo("cards")'))
            or hashlib.sha256(_encode(objects)).hexdigest() != receipt.after_schema_sha256
            or hashlib.sha256(_encode(data)).hexdigest() != receipt.data_sha256
            or data.get("cards", {}).get("count") != receipt.cards
            or connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None):
        raise ValueError("retirement_schema_completion_mismatch")
