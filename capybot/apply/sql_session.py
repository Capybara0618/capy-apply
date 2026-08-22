"""Small PostgreSQL transaction adapter for Apply repositories."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from psycopg.rows import dict_row

from .postgres import engine, upgrade_database


class PostgresRow(dict):
    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class PostgresCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self) -> PostgresRow | None:
        row = self._cursor.fetchone()
        return PostgresRow(row) if row is not None else None

    def fetchall(self) -> list[PostgresRow]:
        return [PostgresRow(row) for row in self._cursor.fetchall()]


class PostgresSession:
    def __init__(self, url: str | None = None):
        upgrade_database(url)
        self._pooled = engine(url).raw_connection()
        self._conn = self._pooled.driver_connection
        self.total_changes = 0

    def __enter__(self) -> "PostgresSession":
        return self

    def __exit__(self, exc_type: Any, _exc: Any, _tb: Any) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._pooled.close()

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> PostgresCursor:
        translated = _compile_sql(sql)
        cursor = self._conn.cursor(row_factory=dict_row)
        cursor.execute(translated, tuple(params or ()))
        if _is_mutation(sql):
            self.total_changes += max(int(cursor.rowcount or 0), 0)
        return PostgresCursor(cursor)

    def executescript(self, sql: str) -> None:
        # Runtime schema is owned by SQLAlchemy/Alembic.
        upgrade_database()


def _is_mutation(sql: str) -> bool:
    return sql.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}


def _compile_sql(sql: str) -> str:
    """Compile repository qmark placeholders to psycopg's parameter style."""

    return _replace_qmarks(sql.strip())


def _replace_qmarks(sql: str) -> str:
    pieces: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            pieces.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            pieces.append(ch)
        elif ch == "?" and not in_single and not in_double:
            pieces.append("%s")
        else:
            pieces.append(ch)
        i += 1
    return "".join(pieces)
