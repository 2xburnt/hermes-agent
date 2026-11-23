# Kanban: gateway-restart casualties do not spend the failure budget

A gateway restart drains for longer than a Kanban submitter's owner wait (`OWNER_WAIT_S`, 180 s)
and kills managed workers whose pipe to the gateway closes. Both used to be booked as ordinary
crashes: `consecutive_failures` rose, and three identical errors in one tick tripped the breaker
sticky at 1 (`effective_limit: 1`). One restart could park every running card.

## Behavior

- The submitter exits `KANBAN_OWNER_RESTART_EXIT_CODE` (69, EX_UNAVAILABLE) when the owner stayed
  restarting/draining/unreachable for the whole wait (`OwnerRestartTimeout`). A real owner refusal
  still exits 1.
- The dispatcher books exit 69, and a managed worker whose owner recorded `worker_lost: EOFError`,
  as a restart casualty: run outcome `crashed` with run metadata `gateway_restart: true`, card back
  to its source phase, `last_failure_error` stamped, `consecutive_failures` untouched, and excluded
  from the same-error systemic trip.
- A card restart-killed more than `_RESTART_CASUALTY_LIMIT` (5) times in a row is charged as an
  ordinary crash again, so a card that itself takes the gateway down still reaches the breaker.
- `managed_worker_failed` and every other loss reason are unchanged.

## Not covered

The dispatcher's `active_pr` respawn guard can still hold a requeued card that has a recent PR URL
comment; that is a separate gate.

Tests: `tests/hermes_cli/test_kanban_gateway_restart_casualty.py`, plus additions in
`tests/hermes_cli/test_kanban_worker_socket_recovery.py`.
