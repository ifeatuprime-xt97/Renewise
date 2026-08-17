"""
renewise/watcher/worker.py

RQ worker entry point.

Run one worker:
    python -m renewise.watcher.worker

Run multiple workers (recommended for burst handling):
    python -m renewise.watcher.worker --concurrency 4

Each worker process is independent — they share only Redis and the SQLite DB.
SQLite's WAL mode (set in schema.py) allows concurrent readers + one writer,
which is sufficient for the write patterns here (one INSERT per payment).

For higher write throughput, swap aiosqlite for asyncpg + PostgreSQL and
update the connection helpers in db/queries.py and watcher/db.py.
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import signal
import sys

import redis as redis_sync
from rq import Queue
from rq.worker import SimpleWorker

from renewise.config import REDIS_URL, RQ_QUEUE_NAME

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


class _NoOpDeathPenalty:
    """
    Drop-in replacement for RQ's UnixSignalDeathPenalty on Windows.
    SIGALRM does not exist on Windows, so we skip the timeout mechanism
    entirely.  Jobs will still complete correctly; they just won't be
    forcibly killed if they exceed a time limit.
    """
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def setup_death_penalty(self):
        pass

    def cancel_death_penalty(self):
        pass


def _run_worker(_: int) -> None:
    """Target function for each worker process."""
    conn = redis_sync.from_url(REDIS_URL)
    queue = Queue(RQ_QUEUE_NAME, connection=conn)

    # Use SimpleWorker on Windows — the default Worker uses os.fork() which
    # does not exist on Windows and will crash immediately.
    # Also override death_penalty_class because RQ's default uses SIGALRM
    # which doesn't exist on Windows either.
    worker = SimpleWorker(
        queues=[queue],
        connection=conn,
        log_job_description=True,
    )
    worker.death_penalty_class = _NoOpDeathPenalty

    log.info("Worker PID=%d started on queue '%s'", os.getpid(), RQ_QUEUE_NAME)
    worker.work(with_scheduler=False)



def main() -> None:
    parser = argparse.ArgumentParser(description="renewise RQ worker")
    parser.add_argument(
        "--concurrency", "-c",
        type=int,
        default=1,
        help="Number of worker processes to spawn (default: 1)",
    )
    args = parser.parse_args()

    if args.concurrency == 1:
        _run_worker(0)
        return

    log.info("Spawning %d worker processes", args.concurrency)
    processes: list[multiprocessing.Process] = []

    def _shutdown(sig, frame):
        log.info("Shutdown signal received — stopping workers")
        for p in processes:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    for i in range(args.concurrency):
        p = multiprocessing.Process(target=_run_worker, args=(i,), daemon=True)
        p.start()
        processes.append(p)
        log.info("Worker process %d started (PID=%d)", i, p.pid)

    for p in processes:
        p.join()


if __name__ == "__main__":
    main()
