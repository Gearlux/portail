# portaide — backlog

Open work for this project. Cross-cutting / multi-project initiatives live in the
workspace root `TASKS.md`. Completed items are not archived here — git history is the record.

- [ ] Hoist `LLMSafeMCPServer` into liquifai together with the other copies (root `TASKS.md`). @low
- [ ] **A stop finds its processes by port NUMBER, so a second listener on the same port at another address is stopped too** @low @bug — measured 2026-10-02 with two listeners on one port, one bound to 127.0.0.1 and one to 0.0.0.0: `pids_listening(port)` answered both pids, so a stop of the server on 127.0.0.1 would also SIGTERM the 0.0.0.0 one (that second bind succeeds only when the app bound first — a start refuses an occupied port). Fix: keep only the pids whose listening address is the server's own (`lsof -iTCP@<host>:<port>` alone still matches every listener when the server is on 0.0.0.0). @2026-10-02
