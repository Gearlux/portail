"""JOBS — one mechanism for every run that walks the records or takes long, with its progress and a stop.

A job is one unit of background work the server keeps: an id, the key it runs under (one job per key at a time),
a title for the page, what it counts (``done`` of ``total`` ``unit``), a message, per-kind ``counts``, and its
ending — the work's result, an error, or ``stopped`` once the work saw the stop flag. ``Jobs`` starts each in a
thread, answers the running job to a second start under its key, stops cooperatively (the flag is read between
items; never undo), and lists the running ones with the last few ended. ``Job.report`` has the shape of a
``(value, total, desc)`` progress callback, so a processor reports per record through the hook it already has.
"""

import json
import threading
from typing import Any, Callable, Dict, List

import pytest

from portail.jobs import KEPT, Job, Jobs, JobStopped

#: Far longer than any job here takes; every wait is bounded so a hang is a failure, never a stuck suite.
A_WHILE = 10.0


def _held_work(released: threading.Event, inside: threading.Event) -> Callable[[Job], Dict[str, Any]]:
    """Work that says it is inside, then waits to be released — the test decides when the job may end."""

    def work(job: Job) -> Dict[str, Any]:
        inside.set()
        assert released.wait(A_WHILE), "the test never released the work"
        return {"message": "held work done"}

    return work


class TestAJob:
    def test_its_state_is_the_json_the_page_reads(self) -> None:
        job = Job("j1", "check:labels", "checking the labels", unit="records", total=2, detail={"label": "cat"})
        state = job.state()
        assert isinstance(state.pop("started"), str) and isinstance(state.pop("elapsed_s"), float)
        assert state == {
            "id": "j1",
            "key": "check:labels",
            "title": "checking the labels",
            "unit": "records",
            "done": 0,
            "total": 2,
            "message": "",
            "counts": {},
            "detail": {"label": "cat"},
            "status": "running",
            "running": True,
            "stop": False,
            "stopped": False,
            "error": "",
            "result": None,
            "ended": None,
        }
        json.dumps(job.state())  # what a route answers must serialize as it is

    def test_a_step_sets_or_advances_the_count_and_merges_what_it_is_told(self) -> None:
        job = Job("j1", "k", "t")
        job.step(3)
        assert job.done == 3 and job.total is None
        job.step()
        assert job.done == 4, "a bare step is one more"
        job.step(total=5, message="rec-b")
        assert (job.done, job.total, job.message) == (4, 5, "rec-b")
        job.step(checked=1)
        job.step(confirmed=1)
        assert job.counts == {"checked": 1, "confirmed": 1}, "counts merge; a step never drops another's"

    def test_report_takes_recordstreams_progress_callback_shape(self) -> None:
        """``DatasetProcessor.run`` calls its callback ``(value, total, desc)`` per record — the job IS that
        callback, so a processor reports with no change of its own."""
        job = Job("j1", "k", "t", unit="records")
        job.report(2.0, 5.0, "DatasetProcessor")
        assert (job.done, job.total) == (2, 5)
        assert job.message == "", "the processor's description is its class name, never the strip's message"

    def test_a_stop_is_seen_between_items_and_marks_the_job_stopped(self) -> None:
        job = Job("j1", "k", "t")
        assert job.stop_asked is False and job.stopped is False
        job.stop = True
        assert job.stop_asked is True, "the work reads the flag…"
        assert job.stopped is True, "…and reading it set is what makes the job 'stopped': the work ended there"

    def test_CON_a_stop_never_read_is_not_a_stopped_job(self) -> None:
        """A stop asked after the last item: the work finished on its own, and says so."""
        job = Job("j1", "k", "t")
        job.stop = True
        assert job.stopped is False


class TestJobsRun:
    def test_a_job_runs_its_work_in_the_background_and_keeps_the_result(self) -> None:
        jobs = Jobs()
        job = jobs.start("k", "the work", lambda job: {"message": "3 record(s) done", "written": 3})
        assert job.id == "j1" and jobs.get("j1") is job
        assert job.wait(A_WHILE), "the work never ended"
        state = job.state()
        assert state["running"] is False and state["ended"] is not None
        assert state["result"] == {"message": "3 record(s) done", "written": 3}
        assert state["message"] == "3 record(s) done", "a result's message is the job's — what the toast says"
        assert state["error"] == "" and state["stopped"] is False

    def test_the_work_steps_the_job_it_is_handed(self) -> None:
        def work(job: Job) -> Dict[str, Any]:
            for n in range(1, 4):
                job.step(n, total=3, message=f"rec-{n}")
            return {}

        job = Jobs().start("k", "t", work)
        assert job.wait(A_WHILE)
        assert (job.done, job.total, job.message) == (3, 3, "rec-3")

    def test_a_result_without_a_message_leaves_the_jobs_message(self) -> None:
        def work(job: Job) -> Dict[str, Any]:
            job.step(message="the last thing it said")
            return {"written": 0}

        job = Jobs().start("k", "t", work)
        assert job.wait(A_WHILE)
        assert job.message == "the last thing it said" and job.result == {"written": 0}

    def test_ids_count_up_per_registry(self) -> None:
        jobs = Jobs()
        first = jobs.start("a", "t", lambda job: None)
        second = jobs.start("b", "t", lambda job: None)
        assert (first.id, second.id) == ("j1", "j2")
        assert first.wait(A_WHILE) and second.wait(A_WHILE)

    def test_a_failing_work_ends_the_job_with_its_error_and_a_message(self) -> None:
        def work(job: Job) -> Dict[str, Any]:
            job.step(2, total=5)
            raise ValueError("the chain raised")

        job = Jobs().start("k", "tint", work)
        assert job.wait(A_WHILE)
        state = job.state()
        assert state["running"] is False and state["error"] == "ValueError: the chain raised"
        assert state["message"] == "tint failed: the chain raised"
        assert state["result"] is None and (state["done"], state["total"]) == (2, 5), "what it counted stays"

    def test_the_finished_hook_runs_after_the_work_while_the_job_still_runs(self) -> None:
        """What a route hangs on a run's END (a tool's history is written once the run finished, never on the ALL
        question or a refusal) runs in the job's thread, after the work, before the job is reported ended."""
        seen: List[Any] = []

        def finished(job: Job) -> None:
            seen.append((job.running, job.result))

        job = Jobs().start("k", "t", lambda job: {"message": "ok"}, finished=finished)
        assert job.wait(A_WHILE)
        assert seen == [(True, {"message": "ok"})]

    def test_CON_the_finished_hook_is_skipped_when_the_work_failed(self) -> None:
        seen: List[Any] = []
        job = Jobs().start("k", "t", lambda job: 1 / 0, finished=lambda job: seen.append(job.id))
        assert job.wait(A_WHILE)
        assert seen == [] and job.error.startswith("ZeroDivisionError")

    def test_a_finished_hook_that_raises_is_the_jobs_error_and_the_result_is_kept(self) -> None:
        def finished(job: Job) -> None:
            raise RuntimeError("the history file is not a map")

        job = Jobs().start("k", "tint", lambda job: {"message": "ok"}, finished=finished)
        assert job.wait(A_WHILE)
        assert job.result == {"message": "ok"} and job.error == "RuntimeError: the history file is not a map"

    def test_a_second_start_under_a_running_key_answers_that_job_and_starts_nothing(self) -> None:
        released, inside = threading.Event(), threading.Event()
        calls: List[int] = []

        def work(job: Job) -> Dict[str, Any]:
            calls.append(1)
            return _held_work(released, inside)(job)

        jobs = Jobs()
        first = jobs.start("model:a", "a", work)
        assert inside.wait(A_WHILE)
        again = jobs.start("model:a", "a", work)
        assert again is first and calls == [1], "the running job is the answer; nothing ran twice"
        assert jobs.running("model:a") is first
        released.set()
        assert first.wait(A_WHILE)
        assert jobs.running("model:a") is None

    def test_once_a_key_ended_a_new_start_under_it_is_a_new_job(self) -> None:
        jobs = Jobs()
        first = jobs.start("k", "t", lambda job: None)
        assert first.wait(A_WHILE)
        second = jobs.start("k", "t", lambda job: None)
        assert second is not first and second.id == "j2"
        assert second.wait(A_WHILE)

    def test_a_stop_is_cooperative_and_keeps_what_was_done(self) -> None:
        """The flag is set at once and answered at once; the work reads it between items, finishes the item in
        hand, and ends — nothing it did is undone."""
        first_done, go_on = threading.Event(), threading.Event()
        done: List[int] = []

        def work(job: Job) -> Dict[str, Any]:
            for n in range(1, 4):
                if job.stop_asked:
                    break
                done.append(n)
                job.step(n, total=3)
                if n == 1:
                    first_done.set()
                    assert go_on.wait(A_WHILE)
            return {"message": f"stopped after {len(done)}" if job.stopped else "ran to the end"}

        jobs = Jobs()
        job = jobs.start("k", "t", work)
        assert first_done.wait(A_WHILE)
        state = jobs.stop("j1").state()
        assert state["stop"] is True and state["running"] is True, "answered at once, before the work noticed"
        go_on.set()
        assert job.wait(A_WHILE)
        assert done == [1] and job.stopped is True and job.message == "stopped after 1"

    def test_stopping_an_ended_job_answers_its_state_unchanged(self) -> None:
        jobs = Jobs()
        job = jobs.start("k", "t", lambda job: {"message": "done"})
        assert job.wait(A_WHILE)
        state = jobs.stop("j1").state()
        assert state["stop"] is False and state["stopped"] is False and state["message"] == "done"

    def test_an_unknown_id_is_a_key_error_naming_the_jobs_there_are(self) -> None:
        jobs = Jobs()
        with pytest.raises(KeyError, match="no job 'j9' — the jobs are: none"):
            jobs.get("j9")
        assert jobs.start("k", "t", lambda job: None).wait(A_WHILE)
        with pytest.raises(KeyError, match="no job 'j9' — the jobs are: j1"):
            jobs.stop("j9")

    def test_the_list_is_the_running_jobs_then_the_ended_newest_first(self) -> None:
        released, inside = threading.Event(), threading.Event()
        jobs = Jobs()
        ended_first = jobs.start("a", "t", lambda job: None)
        assert ended_first.wait(A_WHILE)
        ended_second = jobs.start("b", "t", lambda job: None)
        assert ended_second.wait(A_WHILE)
        running = jobs.start("c", "t", _held_work(released, inside))
        assert inside.wait(A_WHILE)
        assert [(j["id"], j["running"]) for j in jobs.list()] == [
            (running.id, True),
            (ended_second.id, False),
            (ended_first.id, False),
        ]
        released.set()
        assert running.wait(A_WHILE)

    def test_only_the_last_kept_ended_jobs_stay_listed_and_a_running_one_is_never_dropped(self) -> None:
        released, inside = threading.Event(), threading.Event()
        jobs = Jobs()
        running = jobs.start("hold", "t", _held_work(released, inside))
        assert inside.wait(A_WHILE)
        for n in range(KEPT + 1):
            assert jobs.start(f"k{n}", "t", lambda job: None).wait(A_WHILE)
        listed = jobs.list()
        assert len(listed) == KEPT + 1 and listed[0]["id"] == running.id
        assert "j2" not in {j["id"] for j in listed}, "the oldest ended job is gone"
        with pytest.raises(KeyError):
            jobs.get("j2")
        released.set()
        assert running.wait(A_WHILE)


class TestHowAJobEnded:
    """``status`` is the one word for a job's state: running, done, stopped or failed."""

    def test_a_job_that_ran_to_the_end_is_done(self) -> None:
        job = Jobs().start("k", "t", lambda job: None)
        assert job.wait(A_WHILE)
        assert job.state()["status"] == "done"

    def test_a_job_whose_work_raised_is_failed(self) -> None:
        job = Jobs().start("k", "t", lambda job: 1 / 0)
        assert job.wait(A_WHILE)
        assert job.state()["status"] == "failed"

    def test_a_job_whose_work_read_the_stop_flag_and_returned_is_stopped(self) -> None:
        job = Job("j1", "k", "t")
        job.stop = True
        assert job.stop_asked
        job.running = False
        assert job.state()["status"] == "stopped"


class TestCheckStop:
    """``check_stop`` is the stop for a work that would rather be thrown out of its loops than return from each."""

    def test_it_does_nothing_while_no_stop_is_asked(self) -> None:
        job = Job("j1", "k", "t")
        job.check_stop()
        assert job.stopped is False

    def test_it_raises_once_a_stop_is_asked_and_the_job_ends_stopped(self) -> None:
        inside, go_on = threading.Event(), threading.Event()
        done: List[int] = []

        def work(job: Job) -> Dict[str, Any]:
            for n in range(1, 4):
                job.check_stop()
                done.append(n)
                job.step(n, total=3)
                inside.set()
                assert go_on.wait(A_WHILE)
            return {"message": "ran to the end"}

        jobs = Jobs()
        job = jobs.start("k", "the walk", work)
        assert inside.wait(A_WHILE)
        jobs.stop(job.id)
        go_on.set()
        assert job.wait(A_WHILE)
        state = job.state()
        assert done == [1] and (state["status"], state["stopped"], state["error"]) == ("stopped", True, "")
        assert state["message"] == "the walk stopped" and state["result"] is None
        assert (state["done"], state["total"]) == (1, 3), "what it counted stays"

    def test_the_words_a_stop_is_raised_with_are_the_jobs_message(self) -> None:
        def work(job: Job) -> None:
            job.stop = True
            job.check_stop("stopped — nothing was written")

        job = Jobs().start("k", "t", work)
        assert job.wait(A_WHILE)
        assert job.state()["message"] == "stopped — nothing was written"

    def test_CON_the_finished_hook_is_skipped_for_a_stopped_work(self) -> None:
        seen: List[str] = []

        def work(job: Job) -> None:
            raise JobStopped()

        job = Jobs().start("k", "t", work, finished=lambda job: seen.append(job.id))
        assert job.wait(A_WHILE)
        assert seen == [] and job.stopped is True
