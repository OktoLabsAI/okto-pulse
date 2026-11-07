"""Immutable historical contracts; rolling ORM metadata never defines old formats."""

import gzip
from importlib.resources import files
import json

from .current_relational_schema import APPLICATION_ID, RelationalSchemaContract, StorageFormatError, _SCHEMA_QUERY

FORMAT406_SHA256 = "945cadedcd28c3f8fd196f13eb82037b5f3e2b8f19d3da5e43f4ab6bb6f5a9c0"
FORMAT405_SHA256 = "9f69fa459c17edd0f0ff2537445fe4d8dc6ed61ff610ccd819b2aa55fb0c4929"
FORMAT407_SHA256 = "0d30bff9f25fa2135290bd7bfb79dea0fb8ee125a61704fb099b2ffee2bae25e"
FORMAT406_NEW_TABLES = frozenset({"task_requirement_decisions", "task_requirement_decision_operations"})


def historical_schema_contract(version=406):
    if version == 407:
        payload = json.loads(gzip.decompress(files(__package__).joinpath('format407_schema.json.gz').read_bytes()))
        contract = RelationalSchemaContract(tuple(tuple(row) for row in payload['objects']))
        if payload['schema_version'] != 407 or payload['schema_sha256'] != FORMAT407_SHA256 or contract.sha256 != FORMAT407_SHA256:
            raise StorageFormatError('historical_contract')
        return contract
    payload = json.loads(gzip.decompress(files(__package__).joinpath("format406_schema.json.gz").read_bytes()))
    contract = RelationalSchemaContract(tuple(tuple(row) for row in payload["objects"]))
    if payload["schema_version"] != 406 or payload["schema_sha256"] != FORMAT406_SHA256 or contract.sha256 != FORMAT406_SHA256:
        raise StorageFormatError("historical_contract")
    if version == 405:
        contract = RelationalSchemaContract(tuple(row for row in contract.objects if row[2] not in FORMAT406_NEW_TABLES))
        if contract.sha256 != FORMAT405_SHA256:
            raise StorageFormatError("historical_contract")
    elif version != 406:
        raise StorageFormatError("historical_version")
    return contract


def inspect_historical_schema(cursor, contract=None, *, version=406):
    expected = historical_schema_contract(version)
    if contract is not None and contract.objects != expected.objects:
        raise StorageFormatError("historical_contract")
    if cursor.execute("PRAGMA main.application_id").fetchone()[0] != APPLICATION_ID:
        raise StorageFormatError("application_id")
    if cursor.execute("PRAGMA main.user_version").fetchone()[0] != version:
        raise StorageFormatError("version")
    if tuple(tuple(row) for row in cursor.execute(_SCHEMA_QUERY)) != expected.objects:
        raise StorageFormatError("schema_fingerprint")
    if list(cursor.execute("PRAGMA main.quick_check(1)")) != [("ok",)]:
        raise StorageFormatError("database_integrity")
    if cursor.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
        raise StorageFormatError("foreign_key_integrity")
    return False
