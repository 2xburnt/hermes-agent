# Hetz local Kanban deep links

Task: t_4306f854. These are local patches, not upstream features.

URL contracts (substitute the real task ID):
- WebUI: https://hermes.ison-acrux.ts.net/?panel=kanban&task=t_0123abcd
- Dashboard: https://twice.ison-acrux.ts.net/kanban?task=t_0123abcd

Both retain the existing selected board/profile. No auth bypass is added. WebUI
login preserves the full target in `next`; malformed IDs are ignored. A valid
but missing card uses each UI's existing error display.

## Agent/dashboard update retention

The dashboard's `plugins/kanban/dashboard/dist/index.js` IS source: a plain,
hand-written IIFE with no build step or alternate TS source. It is carried in
a signed `local(hetz):` commit on the existing Agent mirror patch stack:
`~/src/hermes-agent.git` main -> live `~/.hermes/hermes-agent` origin/main.

Add `tests/plugins/test_kanban_deep_link.py` to mirror `hetz.test`. It invokes
`node --test tests/plugins/kanban_deep_link.test.cjs` against the restacked tree.
Follow the existing hermes-restack adoption/rebase/test/publish procedure. Do
not reset to upstream directly: `hermes update` must fetch the local mirror.

## Separate WebUI repository

WebUI lives in `~/src/hermes-webui`, not in the Agent checkout, so `hermes
update` does not carry its Git history. Its signed `local(hetz):` commit is on
local master. Update WebUI with `git pull --rebase origin master`, never a hard
reset to origin/master. Preserve that commit just like the Agent local stack.

A recovery copy, including its tests and user-facing docs, is tracked in the
Agent stack at `scripts/local-webui-kanban-deep-links.patch`. If a fresh WebUI
checkout loses the commit, with a CLEAN working tree run:

    git -C ~/src/hermes-webui am -3 --gpg-sign \
      ~/.hermes/hermes-agent/scripts/local-webui-kanban-deep-links.patch
    node --test ~/src/hermes-webui/tests/kanban_deep_link.test.cjs

If the patch conflicts, stop and resolve it; do not drop it silently. If the
same functionality has landed upstream, verify the actual links before
removing this recovery patch and its restack check. The installed
`~/.local/bin/hermes-restack` checks the WebUI test at the start of each run,
including --no-update; a missing test/patch is a hard failure with this recipe.

## Verification

    node --test tests/plugins/kanban_deep_link.test.cjs
    node --test ~/src/hermes-webui/tests/kanban_deep_link.test.cjs

Open both URL shapes with an existing card. Check refresh, desktop/mobile,
missing and invalid task IDs, and an ordinary URL without `task`. The WebUI
explicit card target must win over localStorage's saved chat. Its login
redirect must retain both `panel` and `task`.

Static JS changes require a fresh page load, not a gateway restart. Do not
restart hermes-gateway without telling CommBot first. The existing Agent HEAD
watcher may restart only hermes-webui after an Agent commit is deployed.
