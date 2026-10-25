"""Current required evidence, not superseded same-head suites, decides acceptance.

Collector-level tests exercise the real gh transport/JSON parser with explicit
fixtures; no network or ambient credentials. See also closed, unmerged #122701.
"""
import copy
import json
import subprocess

import pytest

from hermes_cli import kanban_pr_acceptance as acceptance

SHA = "a" * 40
URL = "https://github.com/acme/repo/pull/7"


def check(run_id, conclusion="success", *, app=1, status="completed", **fields):
    return {"id": run_id, "name": "required", "head_sha": SHA,
            "app": {"id": app}, "status": status, "conclusion": conclusion,
            "html_url": f"https://github.com/acme/repo/checks/{run_id}", **fields}


@pytest.fixture
def evidence(monkeypatch):
    state = {"runs": [], "app": 1, "statuses": [], "head": SHA,
             "base": "main", "workflows": [], "calls": []}

    def transport(command, **kwargs):
        endpoint = command[2]
        state["calls"].append((endpoint, command, kwargs))
        if state.get("fault_at") and state["fault_at"] in endpoint:
            raise state["fault"]
        if endpoint == "graphql":
            value = {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": "main", "state": "OPEN",
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                    {"context": "required", "app": {"databaseId": state["app"]}}]}}}}}}
        elif "/rules/branches/" in endpoint:
            value = state.get("rules", [[]])
        elif "/check-runs?" in endpoint:
            total = state.get("check_total", len(state["runs"]))
            value = [{"total_count": total, "check_runs": [r]} for r in state["runs"]]
            value = value or [{"total_count": total, "check_runs": []}]
            value = state.get("check_pages", value)
        elif "/statuses?" in endpoint:
            value = [[s] for s in state["statuses"]] or [[]]
        elif "/actions/runs?" in endpoint:
            total = state.get("workflow_total", len(state["workflows"]))
            value = [{"total_count": total, "workflow_runs": [w]} for w in state["workflows"]]
            value = value or [{"total_count": total, "workflow_runs": []}]
            value = state.get("workflow_pages", value)
        elif "/pulls/" in endpoint:
            value = {"head": {"sha": state["head"]}, "base": {"ref": state["base"]}, "state": "open"}
        else:
            raise AssertionError(endpoint)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(value))

    monkeypatch.setattr(acceptance.subprocess, "run", transport)
    return state


def collect():
    return acceptance.collect_acceptance("acme/repo", URL)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("old", ["skipped", "failure", "cancelled"])
def test_new_success_supersedes_same_app_history(evidence, reverse, old):
    evidence["runs"] = [check(10, old), check(20)][::-1 if reverse else 1]
    receipt = collect()
    assert receipt["ok"], receipt
    assert [c["id"] for c in receipt["checks"]] == [20]


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_completed_required_job_uses_github_passing_semantics(evidence, conclusion):
    evidence["runs"] = [check(20, conclusion)]
    receipt = collect()
    assert receipt["ok"], receipt
    assert receipt["checks"][0]["conclusion"] == conclusion


def action_check(run_id, suite, conclusion="success", **fields):
    return check(run_id, conclusion, app=15368, check_suite={"id": suite}, **fields)


def workflow(run_id, suite, *, event="pull_request", workflow_id=7, **fields):
    return {"id": run_id, "check_suite_id": suite, "workflow_id": workflow_id,
            "head_sha": SHA, "event": event, "run_attempt": 1,
            "status": "completed", "conclusion": "success", "pull_requests": [],
            "html_url": f"https://github.com/acme/repo/actions/runs/{run_id}", **fields}


@pytest.mark.parametrize("event", ["workflow_dispatch", "pull_request_target", "workflow_run", "schedule"])
def test_actions_evidence_requires_head_ci_event(evidence, event):
    evidence.update(app=15368, runs=[action_check(20, 200)],
                    workflows=[workflow(2, 200, event=event)])
    receipt = collect()
    assert not receipt["ok"], receipt
    assert receipt["classification"] == "missing"


def test_delayed_old_job_id_cannot_override_new_workflow_failure(evidence):
    evidence.update(app=15368,
                    runs=[action_check(99, 100), action_check(20, 200, "failure")],
                    workflows=[workflow(1, 100), workflow(2, 200)])
    receipt = collect()
    assert receipt["classification"] == "failure", receipt
    assert [c["id"] for c in receipt["checks"]] == [20]


@pytest.mark.parametrize("status,expected", [("queued", "pending"), ("in_progress", "pending"), ("completed", "missing")])
def test_new_workflow_without_required_job_cannot_reuse_old_success(evidence, status, expected):
    evidence.update(app=15368, runs=[action_check(10, 100)],
                    workflows=[workflow(1, 100), workflow(2, 200, status=status)])
    receipt = collect()
    assert receipt["classification"] == expected, receipt
    assert not receipt["ok"]


@pytest.mark.parametrize("conclusion,status,expected", [
    ("failure", "completed", "failure"), (None, "queued", "pending"),
    (None, "in_progress", "pending"), ("skipped", "queued", "pending"),
    ("cancelled", "completed", "infra"), ("timed_out", "completed", "infra"),
    ("action_required", "completed", "infra"), (None, "completed", "infra"),
    ("unknown", "completed", "infra")])
@pytest.mark.parametrize("actions", [False, True])
def test_newer_nonpassing_result_cannot_hide_behind_old_success(evidence, conclusion, status, expected, actions):
    if actions:
        evidence.update(app=15368, runs=[action_check(10, 100), action_check(20, 200, conclusion, status=status)],
                        workflows=[workflow(1, 100), workflow(2, 200)])
    else:
        evidence["runs"] = [check(10), check(20, conclusion, status=status)]
    receipt = collect()
    assert receipt["classification"] == expected, receipt
    assert receipt["checks"][0]["id"] == 20


@pytest.mark.parametrize("app", [None, -1])
@pytest.mark.parametrize("source", ["status", "check"])
def test_same_named_check_and_status_must_both_pass(evidence, app, source):
    evidence.update(app=app, runs=[check(20, "failure" if source == "check" else "success")],
                    statuses=[{"id": 30, "context": "required", "state": "failure" if source == "status" else "success"}])
    receipt = collect()
    assert receipt["classification"] == "failure", receipt
    assert {c["source"] for c in receipt["checks"]} == {"check_run", "status"}


@pytest.mark.parametrize("state,expected", [("success", "success"), ("failure", "failure"), ("pending", "pending"),
                                            ("skipped", "infra"), ("neutral", "infra"), ("error", "infra")])
def test_latest_legacy_status_has_independent_strict_semantics(evidence, state, expected):
    evidence.update(app=None, statuses=[{"id": 5, "context": "required", "state": "success"},
                                        {"id": 10, "context": "required", "state": state}])
    assert collect()["classification"] == expected


@pytest.mark.parametrize("app", [None, -1, 1])
def test_wrong_app_cannot_replace_pinned_failure_or_hide_unpinned_app_failure(evidence, app):
    evidence.update(app=app, runs=[check(10, "failure"), check(20, app=2)])
    receipt = collect()
    assert receipt["classification"] == "failure", receipt


def test_pinned_context_rejects_wrong_app_and_legacy_success(evidence):
    evidence.update(runs=[check(20, app=2)], statuses=[{"id": 30, "context": "required", "state": "success"}])
    # Upstream retains an app-pinned context's status as unverifiable evidence; it still never passes.
    receipt = collect()
    assert receipt["classification"] == "unverifiable_app" and not receipt["ok"]


@pytest.mark.parametrize("fault", ["head", "base", "stale", "missing"])
@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_passing_outcomes_do_not_waive_exact_head_base_and_presence(evidence, fault, conclusion):
    evidence["runs"] = [check(20, conclusion)]
    if fault == "stale":
        evidence["runs"][0]["head_sha"] = "b" * 40
    elif fault == "missing":
        evidence["runs"] = []
    else:
        evidence[fault] = "b" * 40 if fault == "head" else "release"
    assert collect()["classification"] == ("missing" if fault == "missing" else "stale")


@pytest.mark.parametrize("event", ["pull_request", "push", "merge_group"])
@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_actions_job_can_pass_without_passing_whole_suite_or_pr_association(evidence, event, conclusion):
    evidence.update(app=15368, runs=[action_check(20, 200, conclusion)],
                    workflows=[workflow(2, 200, event=event, conclusion="failure", pull_requests=[])])
    receipt = collect()
    assert receipt["ok"], receipt
    assert receipt["checks"][0]["conclusion"] == conclusion


@pytest.mark.parametrize("fault", ["check_total", "workflow_total", "no_workflow", "workflow_head", "unknown_suite"])
def test_incomplete_or_mismatched_actions_evidence_fails_closed(evidence, fault):
    evidence.update(app=15368, runs=[action_check(20, 200)], workflows=[workflow(2, 200)])
    if fault in {"check_total", "workflow_total"}:
        evidence[fault] = 2
    elif fault == "no_workflow":
        evidence["workflows"] = []
    elif fault == "workflow_head":
        evidence["workflows"][0]["head_sha"] = "b" * 40
    else:
        evidence["runs"][0]["check_suite"]["id"] = 999
    assert collect()["classification"] == "infra"


def test_rules_and_both_actions_pages_are_complete_and_profile_scoped(evidence, monkeypatch):
    evidence.update(app=None, runs=[action_check(10, 100, "failure"), action_check(20, 200, "skipped")],
                    workflows=[workflow(1, 100), workflow(2, 200)],
                    rules=[[], [{"type": "required_status_checks", "parameters": {
                        "required_status_checks": [{"context": "required", "integration_id": 15368}]}}]])
    monkeypatch.setattr(acceptance, "_assignee_profile_home", lambda assignee: "/isolated/profile")
    monkeypatch.setattr(acceptance, "_gh_env", lambda home: {"TARGET_HOME": home})
    receipt = acceptance.collect_acceptance("acme/repo", URL, assignee="worker")
    assert receipt["ok"], receipt
    assert {c["id"] for c in receipt["checks"]} == {20}
    for endpoint, command, kwargs in evidence["calls"]:
        assert kwargs["env"] == {"TARGET_HOME": "/isolated/profile"}
        if "?" in endpoint:
            assert "--paginate" in command and "--slurp" in command
    assert {r["app_id"] for r in receipt["required"]} == {None, 15368}


@pytest.mark.parametrize("endpoint", ["graphql", "/rules/", "/check-runs?", "/statuses?", "/actions/runs?", "/pulls/"])
@pytest.mark.parametrize("code,expected", [(1, "infra"), (4, "auth")])
def test_api_and_auth_faults_never_become_acceptance(evidence, endpoint, code, expected):
    evidence.update(app=15368, runs=[action_check(20, 200)], workflows=[workflow(2, 200)],
                    fault_at=endpoint, fault=subprocess.CalledProcessError(code, ["gh"], stderr="private token value"))
    receipt = collect()
    assert receipt["classification"] == expected, receipt
    assert "private token value" not in json.dumps(receipt)


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_http_auth_denial_is_sanitized(evidence, status_code):
    evidence.update(fault_at="graphql", fault=subprocess.CalledProcessError(
        1, ["gh"], stderr=f"token secret HTTP {status_code}"))
    receipt = collect()
    assert receipt["classification"] == "auth"
    assert "token secret" not in json.dumps(receipt)


def test_unrelated_optional_workflow_does_not_veto_required_job(evidence):
    evidence.update(app=15368, runs=[action_check(20, 200)],
                    workflows=[workflow(2, 200), workflow(3, 300, workflow_id=8, status="queued")])
    assert collect()["ok"]


@pytest.mark.parametrize("conclusion,status,expected", [("failure", "completed", "failure"), (None, "in_progress", "pending")])
def test_independent_workflow_success_cannot_hide_current_failure(evidence, conclusion, status, expected):
    evidence.update(app=15368, runs=[action_check(10, 100, "skipped"),
                    action_check(20, 200, conclusion, status=status), action_check(30, 300)],
                    workflows=[workflow(1, 100), workflow(2, 200), workflow(3, 300, workflow_id=8)])
    receipt = collect()
    assert receipt["classification"] == expected, receipt
    assert {c["id"] for c in receipt["checks"]} == {20, 30}


def test_queued_rerun_cannot_reuse_previous_attempt_success(evidence):
    evidence.update(app=15368, runs=[action_check(20, 200)],
                    workflows=[workflow(2, 200, run_attempt=2, status="queued")])
    receipt = collect()
    assert receipt["classification"] == "pending", receipt
    assert receipt["checks"][0]["source"] == "workflow"


def test_workflow_search_at_api_cap_is_not_proof_of_completeness(evidence):
    evidence.update(app=15368, runs=[action_check(20, 200)],
                    workflows=[workflow(i, i, workflow_id=i) for i in range(1, 1001)])
    assert collect()["classification"] == "infra"


@pytest.mark.parametrize("kind,key,record", [("check", "check_runs", action_check(20, 200)),
                                            ("workflow", "workflow_runs", workflow(2, 200))])
@pytest.mark.parametrize("fault", ["count_race", "duplicate"])
def test_pagination_count_race_or_duplicate_is_not_complete(evidence, kind, key, record, fault):
    evidence.update(app=15368, runs=[action_check(20, 200)], workflows=[workflow(2, 200)])
    pages = [{"total_count": 1, key: [copy.deepcopy(record)]},
             {"total_count": 2 if fault == "count_race" else 1,
              key: [] if fault == "count_race" else [copy.deepcopy(record)]}]
    evidence[kind + "_pages"] = pages
    assert collect()["classification"] == "infra"


@pytest.mark.parametrize("kind", ["check", "workflow"])
@pytest.mark.parametrize("payload", [{}, [], [{"total_count": 1}], [{"total_count": 1, "check_runs": None, "workflow_runs": None}]])
def test_malformed_paginated_evidence_is_infra(evidence, kind, payload):
    evidence.update(app=15368, runs=[action_check(20, 200)], workflows=[workflow(2, 200)])
    evidence[kind + "_pages"] = payload
    assert collect()["classification"] == "infra"
