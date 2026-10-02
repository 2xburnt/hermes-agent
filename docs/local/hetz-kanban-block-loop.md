# Hetz: preserve block-loop triage escalations

Task: t_6c038873. Related local update checks: [Kanban deep links](hetz-kanban-deep-links.md).

`kanban_decompose.list_triage_ids()` is the automatic dispatcher's discovery
path. It excludes cards with `block_recurrences >= BLOCK_RECURRENCE_LIMIT`.
This includes untyped blocks (`block_kind IS NULL`), which also escalate.
Comments do not clear this guard. Explicit human/dashboard specify and
explicit decomposition remain available; no global auto-decompose toggle is
changed. The persistent recurrence history intentionally survives unblocking
and specifying until successful completion or a different block kind.

## Update retention and verification

Carry the signed fix in the existing local Agent mirror stack, not an
uncommitted edit. Include this regression in the mirror's `hetz.test` list:

    tests/gateway/test_kanban_auto_decompose_block_loop.py

After `hermes update`, run the regression with the test virtualenv:

    venv/bin/python -m pytest -q tests/gateway/test_kanban_auto_decompose_block_loop.py

It blocks twice, escalates to triage, appends a comment, and runs three real
auto-decompose ticks. The escalated card is never attempted; a fresh triage
card still progresses with a per-tick budget of one; human specify still
works. Coverage includes needs_input, capability, transient and untyped
blocks. Related tests: test_kanban_auto_decompose_secret_scope.py,
test_kanban_block_kinds.py, test_kanban_decompose.py and test_kanban_decompose_db.py.

Before reloading the gateway, inspect active workers and notify CommBot.
Use `hermes gateway restart` for a graceful drain; never force an in-flight
turn to stop. Kanban/cron workers in independent systemd scopes survive the
managed gateway restart. Verify the new service PID and successful adapter
startup, then observe the board read-only over at least three dispatcher
ticks after the affected card's next block. The card must stay in triage or
blocked with no new `specified` event (and no automatic new worker run).
Do not unblock, rewrite, complete or archive the waiting card to test this.
Keep `kanban.auto_decompose` enabled for ordinary triage cards.
