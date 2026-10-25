"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""


# Every key gh can read a login from, github.com and GitHub Enterprise alike. All of them are
# dropped from the launcher's env for a routed child; only _GH_GITHUB_LOGIN_KEYS come back.
_GH_LOGIN_KEYS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_CONFIG_DIR",
)
# Every call here targets github.com (`--hostname github.com`, github.com PR URLs), where gh
# ignores the enterprise token vars and GH_HOST. So only these are re-added from the profile, and
# only these prove a login: an enterprise-only profile must still get the fail-closed pin below,
# or gh falls through to the launcher's ~/.config/gh/hosts.yml for github.com.
_GH_GITHUB_LOGIN_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR")
# Not a login, but gh cannot reach github.com without them where the profile's .env sets them
# (a proxy in .env is documented; a routed child loses the launcher's .env copy).
_GH_TRANSPORT_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy",
                      "http_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). gh needs only its own login, so the env keeps
    the provider scrub — ``served_profile_child_env(inherit_credentials=False)``,
    its "caller re-adds the few keys the child needs" contract — and re-adds
    exactly gh's github.com login keys (``_GH_GITHUB_LOGIN_KEYS``) from the target profile's own secret scope
    instead of overlaying the whole scope (#134669: the gh child never needs
    the assignee's provider keys or adapter secrets).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from agent.secret_scope import build_profile_secret_scope
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=False)
    routed = _is_routed_home(profile_home)
    if routed:
        # The launcher's own gh login must not reach the child: GH_TOKEN/GITHUB_TOKEN are
        # already scrubbed by hermes_subprocess_env, but the enterprise trio (GH_HOST,
        # GH_/GITHUB_ENTERPRISE_TOKEN) is on no strip list, and a surviving ambient token
        # would reach the child (#122689, the GHE case). The target's own github.com login keys
        # are re-added from its secret scope below.
        for key in _GH_LOGIN_KEYS:
            base.pop(key, None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=False)
    scope = build_profile_secret_scope(Path(profile_home))
    env.update((key, scope[key]) for key in _GH_GITHUB_LOGIN_KEYS if scope.get(key))
    env.update((key, scope[key]) for key in _GH_TRANSPORT_KEYS if key in scope)  # NO_PROXY= clears
    if routed and not (env.keys() & set(_GH_GITHUB_LOGIN_KEYS)):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        profile_home = _assignee_profile_home(assignee)
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{pullRequest(number:{number:d}){{headRefOid baseRefName state\n            baseRef{{branchProtectionRule{{requiredStatusChecks{{context app{{databaseId}}}}}}}}}}}}}}'
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        if repository is None:
            # A private repo the login cannot read resolves to null, not an error.
            raise _GateAuthError(f"HTTP 404 on graphql {repo}")
        pr = repository["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                     paginate=True, profile_home=profile_home)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                     paginate=True, profile_home=profile_home)
        runs = _complete_runs(pages, "check_runs")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                                       paginate=True, profile_home=profile_home) for s in page]
        actions = [r for r in runs if r["app"]["id"] == 15368 and
                   any(r["name"] == c and a in (None, -1, 15368) for c, a in required)]
        workflows = _actions_workflows(repo, sha, profile_home) if actions else {}
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # Status APIs expose an actor, not the originating integration ID.
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            observed = [s for s in statuses if s["context"] == context]
            latest = max(observed, key=lambda s: s["id"]) if observed else None
            selected = _latest_checks(matching, workflows)
            if latest and app_id in (None, -1):
                selected.append(("status", latest))
            if not selected:
                if latest and app_id is not None and app_id != -1:
                    outcomes.append(_record_unverifiable_status(receipt, latest, sha, app_id))
                else:
                    outcomes.append("missing")
                    receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for source, check in selected:
                is_run = source != "status"
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                if source == "workflow":
                    classification = "missing" if check["status"] == "completed" else "pending"
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"], "source": source,
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except _GateAuthError as exc:
        login = f"assignee profile {assignee!r}'s gh login" if assignee else "the ambient gh login"
        receipt.update(classification="auth",
                       detail=f"GitHub refused the acceptance read ({exc}) as {login}; "
                              "fix that profile's GitHub credentials/access to the repository, then retry completion.")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _record_unverifiable_status(receipt: dict, status: dict, sha: str, app_id: int) -> str:
    """Retain observed status evidence without treating an actor as an App."""
    classification = "unverifiable_app"
    receipt["checks"].append({
        "name": status["context"], "id": status["id"], "url": status.get("target_url"),
        "head_sha": sha, "classification": classification,
        "conclusion": status["state"], "required_app_id": app_id,
    })
    receipt["detail"] = (
        "A required context exists as a commit status, but GitHub's status API does not "
        "expose its originating GitHub App ID; the configured app pin cannot be verified."
    )
    receipt["recovery"] = (
        "Obtain authoritative GitHub App attribution before retrying completion; "
        "do not remove the source pin or force completion. Use kanban_block to retain "
        "the task and workspace while this native capability is unavailable."
    )
    return classification


def _complete_runs(pages: list[dict] | dict, key: str) -> list[dict]:
    if not isinstance(pages, list):
        raise ValueError("Expected paginated run evidence")
    total = pages[0]["total_count"]
    runs = [run for page in pages for run in page[key]]
    if (any(page["total_count"] != total for page in pages) or
            len(runs) != total or len({r["id"] for r in runs}) != total):
        raise ValueError("Incomplete or changing run pagination")
    return runs


def _actions_workflows(repo: str, sha: str, profile_home: str | None) -> dict:
    pages = _api(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100",
                 paginate=True, profile_home=profile_home)
    runs = _complete_runs(pages, "workflow_runs")
    # GitHub caps filtered Actions searches at 1,000 results. At the cap we
    # cannot prove that no newer generation was omitted, even if counts agree.
    if pages[0]["total_count"] >= 1000:
        raise ValueError("Incomplete workflow-run pagination")
    if any(r["head_sha"] != sha for r in runs):
        raise ValueError("Workflow head does not match requested head")
    return {r["check_suite_id"]: r for r in runs}


def _latest_checks(matching: list[dict], workflows: dict) -> list[tuple[str, dict]]:
    by_app = {}
    for run in matching:
        by_app.setdefault(run["app"]["id"], []).append(run)
    selected = []
    for app, checks in by_app.items():
        if app == 15368:  # GitHub Actions: job creation order can lag workflow generation.
            selected.extend(_latest_actions_checks(checks, workflows))
        else:
            selected.append(("check_run", max(checks, key=lambda r: r["id"])))
    return selected


def _latest_actions_checks(checks: list[dict], workflows: dict) -> list[tuple[str, dict]]:
    events = {"push", "pull_request", "merge_group"}
    candidates = [workflows[r["check_suite"]["id"]] for r in checks]
    workflow_ids = {w["workflow_id"] for w in candidates if w["event"] in events}
    latest = {}
    for run in workflows.values():
        workflow_id = run["workflow_id"]
        if workflow_id not in workflow_ids or run["event"] not in events:
            continue
        if workflow_id not in latest or (run["id"], run["run_attempt"]) > (latest[workflow_id]["id"], latest[workflow_id]["run_attempt"]):
            latest[workflow_id] = run
    selected = []
    # Independent workflows cannot supersede one another's same-name failures.
    for newest in latest.values():
        current = [r for r in checks if r["check_suite"]["id"] == newest["check_suite_id"]]
        if not current or (newest["run_attempt"] > 1 and newest["status"] != "completed"):
            # Reruns reuse suite IDs. Wait rather than accept the old attempt's jobs.
            selected.append(("workflow", newest))
        else:
            selected.append(("check_run", max(current, key=lambda r: r["id"])))
    return selected


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    if outcome is None:
        return "infra"
    # GitHub accepts emitted skipped/neutral jobs, not absent workflows or
    # legacy commit statuses with invented states. Keep the raw conclusion.
    if is_run and outcome in {"skipped", "neutral"}:
        return "success"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
