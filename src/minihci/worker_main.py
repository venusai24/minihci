"""Worker process: python -m minihci.worker_main"""

import logging
import signal
import sys
import threading
from types import FrameType

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.pool import NullPool

from minihci import reconcile
from minihci.config import Settings
from minihci.db import make_engine, make_session_factory
from minihci.driver import build_driver
from minihci.ha import heartbeat
from minihci.logging import configure_logging
from minihci.scheduler import collector, rebalancer
from minihci.tasks.recovery import recover_orphans
from minihci.tasks.registry import REGISTRY
from minihci.tasks.worker import Worker

logger = logging.getLogger("minihci.worker_main")


def acquire_singleton(engine: Engine) -> Connection | None:
    """Take the process-wide worker lock, or return None if another worker holds it.

    The lock is session-level, so it lives on a dedicated NON-pooled connection: closing the
    returned connection really closes the DB session, which is what releases the lock (and a
    crashed process releases it the same way).
    """
    conn = create_engine(engine.url, poolclass=NullPool).connect().execution_options(isolation_level="AUTOCOMMIT")
    if conn.execute(text("SELECT pg_try_advisory_lock(0, 0)")).scalar():
        return conn
    conn.close()
    return None


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    configure_logging()
    engine = make_engine(settings.database_url)
    singleton = acquire_singleton(engine)
    if singleton is None:
        logger.error("another worker already holds the singleton lock; exiting")
        return 1
    try:
        session_factory = make_session_factory(engine)
        driver = build_driver(settings)
        report = recover_orphans(session_factory, driver, settings, REGISTRY)
        logger.info("recovery: resumed=%s rolled_back=%s failed=%s", report.resumed, report.rolled_back, report.failed)
        try:
            rep = reconcile.reconcile_once(session_factory, driver, settings)
            logger.info(
                "reconcile: missing=%s drift=%s unknown=%s skipped=%s corrective=%s",
                rep.missing,
                rep.actual_drift,
                rep.unknown,
                rep.skipped,
                rep.corrective,
            )
        except Exception:
            logger.exception("initial reconcile pass failed")
        stop = threading.Event()

        def _stop(signum: int, _frame: FrameType | None) -> None:
            logger.info("signal %d received; stopping", signum)
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        threads = [
            threading.Thread(
                target=Worker(session_factory, driver, settings, REGISTRY).run_forever,
                args=(stop,),
                name=f"worker-{i}",
            )
            for i in range(settings.worker_threads)
        ]
        threads.append(
            threading.Thread(
                target=reconcile.run_forever, args=(stop, session_factory, driver, settings), name="reconciler"
            )
        )
        threads.append(
            threading.Thread(
                target=collector.run_forever, args=(stop, session_factory, driver, settings), name="collector"
            )
        )
        threads.append(
            threading.Thread(target=rebalancer.run_forever, args=(stop, session_factory, settings), name="rebalancer")
        )
        threads.append(
            threading.Thread(
                target=heartbeat.run_forever, args=(stop, session_factory, driver, settings), name="heartbeat"
            )
        )
        for th in threads:
            th.start()
        logger.info("worker started with %d threads", len(threads))
        for th in threads:
            th.join()
        return 0
    finally:
        singleton.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
