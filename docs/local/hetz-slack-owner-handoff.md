# Hetz: scope Slack owner handoffs to ChiefOfBots

Task: t_80f9a4c5. Follow-up to local patch fbba153d502.

The `owner_handoff` exemption in `SlackAdapter._event_declares_bot_sender`
uses ChiefOfBots' current DM, `D0C4S9L5HJL`. The former DM, `D0ADWS3E86S`,
is now Crucible's and is no longer exempt. This replaces the channel literal;
it does not add another allowed channel or broaden the exemption through a
profile's control-channel setting.

All other gates remain unchanged: the event must carry
`metadata.event_type == "owner_handoff"` and its user must be in
`_slack_api_human_users()`. Bot-message subtypes, bot user profiles and the
`users.info` bot-user lookup still reject genuine bot senders. Unmarked
bot-stamped API messages remain bot messages.

## Retention and verification

Carry this signed commit on the existing local `main` patch stack. The mirror's
`hetz.test` list already includes `tests/gateway/test_slack.py`.

Run each test file in its own interpreter from the live checkout:

    venv/bin/python -m pytest -q -p no:cacheprovider tests/gateway/test_slack.py
    venv/bin/python -m pytest -q -p no:cacheprovider tests/gateway/test_slack_api_human_senders.py

The routing regressions verify admission of the marked allowlisted owner in
ChiefOfBots' DM, rejection in Crucible's DM, rejection without the marker or
for another user, and preservation of the existing bot gates.

## Activation

Do not restart the gateway for this patch and do not run an update/restack
that restarts the fleet. ChiefOfBots owns the single coordinated restart with
slagent go-live. Until then the running gateway retains its loaded adapter.
