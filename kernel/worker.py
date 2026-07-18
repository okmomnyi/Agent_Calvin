"""Worker process (Phase 26): drains the job queue.

    python -m kernel.worker            # or: docker compose up --scale worker=3

A separate process from the API on purpose, and for the same reason `bot` is separate: heavy
work (scraping, LLM scoring, CV tailoring, transcription) must not compete with the endpoint
Calvin actually talks to. Run three of these and three jobs run at once -- `FOR UPDATE SKIP
LOCKED` means they never claim the same row.

Deliberately dumb: claim, run, repeat. All retry/backoff/failure policy lives in core.queue,
so this file has no logic worth testing separately and nothing to get out of sync.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time

from core.logging_setup import get_logger
from core.queue import get_queue, registered
from kernel.registry import SkillRegistry

log = get_logger("kernel.worker")

IDLE_SLEEP = float(os.getenv("AGENT_WORKER_IDLE_SLEEP", "2.0"))

_stop = False


def _handle_signal(signum, _frame) -> None:
    """Finish the job in flight, then exit -- `docker compose restart` must not lose work."""
    global _stop
    _stop = True
    log.info("worker: signal %s received, finishing current job then exiting", signum)


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Importing the skills registers their @handler functions as a side effect; without this
    # the worker would claim jobs it has no handler for and fail every one of them.
    SkillRegistry().discover()

    name = f"{socket.gethostname()}:{os.getpid()}"
    log.info("worker %s started — %d handler(s): %s",
             name, len(registered()), ", ".join(sorted(registered())))
    queue = get_queue()

    while not _stop:
        try:
            job = queue.run_one(worker=name)
        except Exception:  # noqa: BLE001 - a DB blip must not kill the worker
            log.exception("worker: claim/run failed; backing off")
            time.sleep(IDLE_SLEEP * 2)
            continue
        if job is None:
            time.sleep(IDLE_SLEEP)

    log.info("worker %s stopped cleanly", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
