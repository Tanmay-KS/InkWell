"""The rename must not orphan a household's existing database."""

from __future__ import annotations

import sqlite3

from agent.memory import hermes_memory
from agent.memory.hermes_memory import HouseholdMemory


def _database(path, marker: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.execute("INSERT INTO marker VALUES (?)", (marker,))
    connection.commit()
    connection.close()


def _marker(memory: HouseholdMemory) -> str:
    assert memory._conn is not None
    return memory._conn.execute("SELECT value FROM marker").fetchone()[0]


def test_the_pre_rename_database_is_moved_once(tmp_path, monkeypatch):
    new = tmp_path / "inkwell_memory.db"
    old = tmp_path / "ledger_memory.db"
    _database(old, "kept")
    monkeypatch.setattr(hermes_memory, "SIDECAR_PATH", new)

    memory = HouseholdMemory()
    memory.connect()
    try:
        assert _marker(memory) == "kept"
    finally:
        memory.close()

    assert new.exists()
    assert not old.exists()


def test_an_existing_inkwell_database_is_never_overwritten(tmp_path, monkeypatch):
    new = tmp_path / "inkwell_memory.db"
    old = tmp_path / "ledger_memory.db"
    _database(new, "new")
    _database(old, "old")
    monkeypatch.setattr(hermes_memory, "SIDECAR_PATH", new)

    memory = HouseholdMemory()
    memory.connect()
    try:
        assert _marker(memory) == "new"
    finally:
        memory.close()

    assert old.exists(), "the legacy file is left alone when the new one exists"
