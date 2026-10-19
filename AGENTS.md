# Rules for agents working on Lazarus
1. Read PRD.md first. Section 6 (SQL contract) is law: do not change its semantics.
2. Raw SQL via psycopg 3. No ORM. All queue SQL lives in app/queue.py.
3. Every job state write after claim is fenced (WHERE id=$1 AND fence=$2 AND status='running'), except the explicit unsafe mode.
4. Use database now() for leases and run_at. Never compare app clocks.
5. No new dependencies without asking. Frontend: no npm, no CDN at runtime (vendored files only).
6. Frontend design must follow 03-ui-ux-palette.md tokens exactly. No Tailwind, no gradients, no emoji icons.
7. Run pytest before reporting a task done. Show the output.
8. Do not add features that are not in PRD.md. If something is missing, ask.
9. Keep functions small; name SQL constants after the transition they implement.
10. Never leave TODO stubs in P0 paths. If you cannot finish, say so.
