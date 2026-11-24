# Jobs

A run that walks every record, or simply takes long, does not belong inside a request: the page waits with nothing
to show, and nobody can stop it. `portail.jobs` runs such work in a thread of its own, counts it, and stops it when
asked.

## Start one

An app keeps one `Jobs` on its workspace:

```python
from portail.jobs import Job, Jobs

jobs = Jobs()

def export(job: Job) -> dict:
    job.step(0, total=len(records))
    for n, record in enumerate(records, start=1):
        if job.stop_asked:          # read between records
            break
        write(record)
        job.step(n)
    return {"message": f"exported {job.done} of {job.total} record(s)"}

job = jobs.start("export", "exporting the records", export)
```

`start` answers at once. The work gets the job, steps it, and returns its result; a result's `message` becomes the
job's message.

| argument | meaning |
| --- | --- |
| `key` | what the job runs under. One job per key runs at a time: a second `start` under a running key starts nothing and answers the job that runs. `jobs.running(key)` tells an app that would rather refuse. |
| `title` | what the page prints |
| `unit`, `total` | what `done` of `total` count (`records` by default); `unit=""` for a run with no count |
| `detail` | whatever the surface that started the job needs to show it |
| `finished` | called with the job after the work SUCCEEDED, in the job's thread, before the job is reported ended |

## Progress

- `job.step()` is one more; `job.step(7)` sets the count; `total=` and `message=` replace theirs.
- `job.step(checked=3, refused=1)` merges per-kind counts (`job.counts`); a step never drops another's.
- `job.report` has the shape of a `(value, total, desc)` progress callback, so a processor that takes one reports
  per record with no change of its own.

## Stop

A stop is cooperative. `jobs.stop(job_id)` sets the flag and answers at once; the work reads it between items, and
what it did is kept. A job never interrupts an op. The work reads the flag one of two ways:

| the work calls | what happens once a stop was asked |
| --- | --- |
| `if job.stop_asked: break` | the work finishes the item in hand, returns its result and message, and the job is `stopped` |
| `job.check_stop()` | `JobStopped` is raised out of its loops; the job is `stopped`, with no result. Its message is `<title> stopped`, or the words given: `job.check_stop("stopped — nothing was written")` |

A stop asked after the last item was never read: the job ran to the end and is `done`.

## What the page reads

`jobs.list()` answers every job's state: the running ones in the order they started, then the last 20 ended ones,
newest first. `job.state()` is one of them:

```json
{"id": "j3", "key": "export", "title": "exporting the records", "unit": "records",
 "done": 40, "total": 120, "message": "", "counts": {}, "detail": {},
 "status": "running", "running": true, "stop": false, "stopped": false,
 "error": "", "result": null,
 "started": "2026-11-24T16:02:11", "ended": null, "elapsed_s": 3.2}
```

`status` is one word: `running`, then `done`, `stopped` or `failed`. A work that raises ends its job `failed`:
`error` is `ValueError: the text`, and `message` is `<title> failed: the text`.

portail mounts no route for jobs. An app routes two requests to its `Jobs`: the list, and the stop by id.
`jobs.get(id)` and `jobs.stop(id)` raise a `KeyError` naming the jobs there are for an id nobody has.

Jobs live in the server's memory. A restart empties the list; what the jobs did stays.
