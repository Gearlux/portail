"""JOBS — the background work a served workspace keeps, with its progress and a cooperative stop.

Every run that walks the records or takes long is ONE :class:`Job` in the workspace's :class:`Jobs`: started at
once in a thread, listed with its count (the one list an app's page polls), stopped by its id. An app keeps a
``Jobs`` on its workspace and routes two requests to it — the list (:meth:`Jobs.list`) and the stop
(:meth:`Jobs.stop`); nothing here knows a route.

A job is domain-free and kind-free: it carries what the page prints (``title``, ``done`` of ``total`` ``unit``, a
``message``), per-kind ``counts`` and ``detail`` for the surface that started it, and its ending — the work's
``result`` (its ``message`` becomes the job's), an ``error``, or ``stopped`` once the work saw the stop flag.
``status`` says the same in one word: ``running``, ``done``, ``stopped`` or ``failed``. :meth:`Job.report` has the
shape of a ``(value, total, desc)`` progress callback, so a processor reports per record through the hook it
already has, with no change of its own.

A stop is COOPERATIVE and keeps what was done: the flag is set and answered at once, and the work reads it between
items — :attr:`Job.stop_asked`, after which it finishes the item in hand and returns, or :meth:`Job.check_stop`,
which throws it out of its loops (:class:`JobStopped`). Nothing it did is undone. A job never interrupts an op.

Jobs live in the server's memory: a restart empties the list, and what the jobs did stays. Never workspace
content, never saved.
"""

import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

from loggair import get_logger

logger = get_logger(__name__)

#: How many ENDED jobs the list keeps (the running ones are never dropped): enough for a page to say the endings
#: it watched after a poll it missed, few enough to stay a short list.
KEPT = 20


class JobStopped(Exception):
    """Raised by :meth:`Job.check_stop` once a stop was asked: the job ends ``stopped``. Its text, when it has
    one, is the job's message."""


class Job:
    """One unit of background work and its state — what ``/api/jobs`` lists.

    Args:
        job_id: Its id in the registry (``j3``).
        key: What it runs under — one job per key at a time (``model:a``, ``export``).
        title: What the page prints.
        unit: What ``done``/``total`` count (``records``, ``annotations``, ``groups``); ``""`` for a run with no
            count.
        total: How many units there are, when known at the start.
        detail: What the surface that started it needs to show it (a label, a record).
    """

    def __init__(
        self,
        job_id: str,
        key: str,
        title: str,
        *,
        unit: str = "records",
        total: Optional[int] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.id = job_id
        self.key = key
        self.title = title
        self.unit = unit
        self.done = 0
        self.total: Optional[int] = total
        self.message = ""
        self.counts: Dict[str, int] = {}
        self.detail: Dict[str, Any] = dict(detail or {})
        self.running = True
        self.stop = False
        self.stopped = False
        self.error = ""
        self.result: Any = None
        self.started = time.time()
        self.ended: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        # `step` runs in the work's thread, `state` in a request's: the counts are merged under a lock so a state
        # read mid-merge never sees half of one.
        self._lock = threading.Lock()

    def step(
        self,
        done: Optional[int] = None,
        *,
        total: Optional[int] = None,
        message: Optional[str] = None,
        counts: Optional[Mapping[str, int]] = None,
        **more: int,
    ) -> None:
        """Advance the job: ``done`` sets the count — a step given nothing at all is one more —, ``total`` and
        ``message`` replace theirs when given, and ``counts`` (a mapping) and ``more`` (keywords) merge into the
        per-kind counts (a step never drops another's). A step given only a total, a message or counts leaves the
        count where it is."""
        with self._lock:
            if done is not None:
                self.done = int(done)
            elif total is None and message is None and not counts and not more:
                self.done += 1
            if total is not None:
                self.total = int(total)
            if message is not None:
                self.message = str(message)
            for name, value in {**dict(counts or {}), **more}.items():
                self.counts[name] = int(value)

    def report(self, value: float, total: float, desc: str = "") -> None:
        """A progress callback, ``(value, total, desc)`` per record — the job IS the callback a
        processor is handed (``set_progress_callback``). The description is the processor's class name, never the
        job's message, so it is ignored."""
        if total and total > 0:
            self.step(int(value), total=int(total))

    @property
    def stop_asked(self) -> bool:
        """Whether a stop was asked — read by the work between items. Reading it set is what makes the job
        ``stopped``: the work ended there, and what it did is kept."""
        if self.stop:
            self.stopped = True
        return self.stop

    def check_stop(self, message: str = "") -> None:
        """Raise :class:`JobStopped` when a stop was asked — the one call between items of a work that would rather
        be thrown out of its loops than return from each. ``message`` becomes the job's (blank: ``<title>
        stopped``)."""
        if self.stop:
            raise JobStopped(message)

    @property
    def status(self) -> str:
        """How the job stands, in one word: ``running``, then ``stopped``, ``failed`` or ``done``."""
        if self.running:
            return "running"
        return "stopped" if self.stopped else ("failed" if self.error else "done")

    @property
    def elapsed_s(self) -> float:
        """How long it ran, or has been running."""
        return (self.ended if self.ended is not None else time.time()) - self.started

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait for the work to end — ``True`` once it has (at once for a job that runs in no thread of its own)."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def state(self) -> Dict[str, Any]:
        """The job as JSON — what a route answers and the page reads."""
        with self._lock:
            counts = dict(self.counts)
        return {
            "id": self.id,
            "key": self.key,
            "title": self.title,
            "unit": self.unit,
            "done": self.done,
            "total": self.total,
            "message": self.message,
            "counts": counts,
            "detail": dict(self.detail),
            "status": self.status,
            "running": self.running,
            "stop": self.stop,
            "stopped": self.stopped,
            "error": self.error,
            "result": self.result,
            "started": datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
            "ended": None if self.ended is None else datetime.fromtimestamp(self.ended).isoformat(timespec="seconds"),
            "elapsed_s": float(self.elapsed_s),
        }


#: The work of a job: called with the job in a thread of its own, steps it, answers its result (a dict whose
#: ``message`` is what the page says).
Work = Callable[[Job], Any]
#: What runs after the work SUCCEEDED, in the job's thread, before the job is reported ended (what an app writes down
#: once a run succeeded); its exception is the job's error, the work's result kept.
Finished = Callable[[Job], None]


class Jobs:
    """The jobs of one served workspace — started here, listed and stopped by id.

    Args:
        kept: How many ended jobs stay listed (:data:`KEPT`).
    """

    def __init__(self, kept: int = KEPT) -> None:
        self.kept = int(kept)
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()
        self._minted = 0

    def start(
        self,
        key: str,
        title: str,
        work: Work,
        *,
        unit: str = "records",
        total: Optional[int] = None,
        detail: Optional[Mapping[str, Any]] = None,
        finished: Optional[Finished] = None,
    ) -> Job:
        """Run ``work`` in a thread of its own as a new job — or answer the job already running under ``key``,
        starting nothing (the page then follows that one)."""
        with self._lock:
            held = self._running(key)
            if held is not None:
                return held
            self._minted += 1
            job = Job(f"j{self._minted}", key, title, unit=unit, total=total, detail=detail)
            self._jobs[job.id] = job
            self._prune()

        def run() -> None:
            try:
                job.result = work(job)
                if finished is not None:
                    finished(job)
            except JobStopped as stop:
                job.stopped = True
                job.message = str(stop) or f"{job.title} stopped"
            except Exception as exc:  # noqa: BLE001 - the job's ending IS the report; nothing else sees it
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = f"{job.title} failed: {exc}"
                logger.warning(f"job {job.id} ({job.title}) failed: {exc}")
                logger.debug(f"job {job.id} traceback", exc_info=True)
            else:
                if isinstance(job.result, Mapping) and isinstance(job.result.get("message"), str):
                    job.message = job.result["message"]
            finally:
                job.ended = time.time()
                job.running = False
                with self._lock:
                    self._prune()

        job._thread = threading.Thread(target=run, name=f"job-{job.id}", daemon=True)
        job._thread.start()
        return job

    def get(self, job_id: str) -> Job:
        """The job ``job_id`` — a ``KeyError`` naming the jobs there are for one nobody has (never started, or
        pruned)."""
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"no job {job_id!r} — the jobs are: {', '.join(self._jobs) or 'none'}")
        return job

    def stop(self, job_id: str) -> Job:
        """Ask ``job_id`` to stop and answer it at once: the work reads the flag between items. A job that already
        ended is answered as it is — a page's stop can always race the end of the job it is offered for."""
        job = self.get(job_id)
        if job.running:
            job.stop = True
        return job

    def running(self, key: str) -> Optional[Job]:
        """The job running under ``key``, if any."""
        with self._lock:
            return self._running(key)

    def list(self) -> List[Dict[str, Any]]:
        """Every job's state: the running ones in the order they started, then the ended ones newest first."""
        with self._lock:
            jobs = list(self._jobs.values())
        running = [job for job in jobs if job.running]
        ended = [job for job in reversed(jobs) if not job.running]
        return [job.state() for job in running + ended]

    def _running(self, key: str) -> Optional[Job]:
        return next((job for job in self._jobs.values() if job.key == key and job.running), None)

    def _prune(self) -> None:
        """Drop the oldest ended jobs beyond ``kept``; a running job is never dropped."""
        ended = [job_id for job_id, job in self._jobs.items() if not job.running]
        for job_id in ended[: max(0, len(ended) - self.kept)]:
            del self._jobs[job_id]


__all__ = ["KEPT", "Finished", "Job", "JobStopped", "Jobs", "Work"]
