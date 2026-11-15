from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text

LOCK_NAMESPACE: dict[str, int] = {"vm": 1, "host": 2}


@contextmanager
def target_lock(engine: Engine, kind: str, target_id: int) -> Iterator[None]:
    """Hold pg_advisory_xact_lock(ns, id) on a dedicated connection until the block exits.

    The lock is transaction-scoped, so a crashed worker releases it with its connection.
    """
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:ns, :id)"), {"ns": LOCK_NAMESPACE[kind], "id": target_id})
        try:
            yield
        finally:
            conn.rollback()  # ends the txn, releasing the lock; nothing was written on this connection
