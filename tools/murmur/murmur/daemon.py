"""Single coordinator process. Polling happens here, never in a model turn."""

import fcntl
from pathlib import Path
import signal
import threading

from . import config
from .control import HostControl
from .hooks import initialize
from .ledger import Ledger


def run(root=None, once=False):
    root = Path(root) if root else config.home()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "coordinator.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("The Murmur coordinator is already running") from error
        stopped = threading.Event()
        if not once:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stopped.set())
        ledger = Ledger(config.ledger_path(root))
        initialize(ledger.db)
        try:
            HostControl(root, ledger, stopped).run(once=once)
        finally:
            ledger.close()
