"""Managed construction must retain local OAuth recovery, without exporting secrets.

Real policy, runtime resolver, AIAgent, pool and auth-store I/O; only vendor
HTTP is replaced. A 401 after construction must refresh the dispatched entry.
"""
import io
import json
import time
import urllib.request
from pathlib import Path

import httpx
import pytest

from tests.gateway.test_managed_turn_contract import assignment


MODEL = 'claude-sonnet-4-5'
OLD = 'sk-ant-oat01-fixture-old'
NEW = 'sk-ant-oat01-fixture-new'


def _frame(tmp_path, monkeypatch):
    frame, _ = assignment(tmp_path, monkeypatch)
    config = json.loads(frame['policy']['config_json'])
    config.update(model={'default': MODEL, 'provider': 'anthropic'},
                  auxiliary={'title_generation': {'enabled': False}},
                  agent={'api_max_retries': 1})
    request = json.loads(frame['policy']['request_json'])
    request.pop('base_url', None)
    request['provider'] = 'anthropic'
    return {**frame, 'api_key': None, 'policy': {**frame['policy'],
        'model': MODEL, 'config_json': json.dumps(config), 'request_json': json.dumps(request)}}


def _seed(tmp_path, monkeypatch):
    root = tmp_path / 'auth-root'
    root.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path / 'fakehome'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'fakehome')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'fakehome'))
    monkeypatch.setenv('HERMES_HOME', str(root))
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    import hermes_constants
    monkeypatch.setattr(hermes_constants, '_default_hermes_root_memo', None)
    rows = [{'id': 'owned', 'source': 'manual:hermes_pkce', 'auth_type': 'oauth',
             'priority': 0, 'access_token': OLD, 'refresh_token': 'fixture-refresh-old',
             'expires_at_ms': int(time.time() * 1000) + 3600000},
            {'id': 'other', 'source': 'manual:other', 'auth_type': 'oauth',
             'priority': 1, 'access_token': 'sk-ant-oat01-fixture-other',
             'refresh_token': 'fixture-refresh-other',
             'expires_at_ms': int(time.time() * 1000) + 3600000}]
    (root / 'auth.json').write_text(json.dumps({'version': 1, 'providers': {},
                                              'credential_pool': {'anthropic': rows}}))
    return root


def _vendor(monkeypatch, before_401=None):
    from agent import anthropic_adapter
    original = anthropic_adapter.build_anthropic_client
    requests, grants = [], []

    def respond(request):
        token = request.headers.get('authorization', '').removeprefix('Bearer ')
        requests.append(token)
        if token == OLD:
            if before_401 is not None:
                before_401()
            return httpx.Response(401, json={'type': 'error', 'error': {
                'type': 'authentication_error', 'message': 'OAuth token has expired'}})
        assert token == NEW
        from tests.fakes.providers.anthropic_messages import Reply, Text, stream_events, _event_bytes
        events = stream_events(Reply(blocks=[Text('RECOVERED')]), [''])
        wire = b''.join(_event_bytes(event) for event in events)
        return httpx.Response(200, content=wire, headers={'content-type': 'text/event-stream'})

    def client(*args, **kwargs):
        return original(*args, **kwargs).with_options(
            http_client=httpx.Client(transport=httpx.MockTransport(respond)), max_retries=0)

    def refresh(request, timeout=None):
        assert request.full_url.endswith('/oauth/token')
        body = json.loads(request.data)
        grants.append(body['refresh_token'])
        assert grants == ['fixture-refresh-old']
        return io.BytesIO(json.dumps({'access_token': NEW,
            'refresh_token': 'fixture-refresh-new', 'expires_in': 3600}).encode())

    monkeypatch.setattr(anthropic_adapter, 'build_anthropic_client', client)
    monkeypatch.setattr(urllib.request, 'urlopen', refresh)
    return requests, grants


def _agent(frame, policy):
    from agent.managed_worker import _construct_agent
    from run_agent import AIAgent
    return _construct_agent(frame, AIAgent, policy, api_key=frame['api_key'],
        model=policy.model, provider=policy.provider, base_url=policy.base_url,
        enabled_toolsets=[], max_iterations=3, skip_context_files=True,
        skip_memory=True, skip_background_review=True, load_soul_identity=False,
        quiet_mode=True, save_trajectories=False)


@pytest.mark.parametrize('inherited,sibling_refresh', [(False, False), (True, False), (True, True)])
def test_managed_agent_refreshes_matching_oauth_entry_after_401(tmp_path, monkeypatch, inherited, sibling_refresh):
    from gateway.session_policy import restore_policy, policy_scope
    root = _seed(tmp_path, monkeypatch)
    profile = root / 'profiles' / 'worker'
    if inherited:
        profile.mkdir(parents=True)
        monkeypatch.setenv('HERMES_HOME', str(profile))
    frame = _frame(tmp_path, monkeypatch)
    durable_before = json.dumps(frame, sort_keys=True)

    def refresh_sibling():
        refreshed = sibling.try_refresh_matching(api_key_hint=OLD)
        assert refreshed is not None and refreshed.access_token == NEW

    requests, grants = _vendor(monkeypatch, refresh_sibling if sibling_refresh else None)
    policy = restore_policy(frame['policy'])
    with policy_scope(policy):
        agent = _agent(frame, policy)
        assert agent.api_key == OLD
        pool = agent._credential_pool
        # Let a missing pool reach the 401 so the regression proves recovery, not just wiring.
        # A shared pool cursor can move after dispatch; the failed key still owns the refresh.
        if pool is not None:
            pool._current_id = 'other'
        if sibling_refresh:
            from agent.credential_pool import load_pool
            sibling = load_pool('anthropic')
        result = agent.run_conversation('Respond with RECOVERED.')
    assert result['final_response'] == 'RECOVERED'
    assert requests == [OLD, NEW]
    assert grants == ['fixture-refresh-old']
    assert agent._credential_pool is not None
    assert agent._credential_pool is pool
    assert agent._credential_pool.current().id == 'owned'
    rows = json.loads((root / 'auth.json').read_text())['credential_pool']['anthropic']
    assert next(row for row in rows if row['id'] == 'owned')['access_token'] == NEW
    assert next(row for row in rows if row['id'] == 'other')['refresh_token'] == 'fixture-refresh-other'
    assert json.dumps(frame, sort_keys=True) == durable_before
    assert OLD not in durable_before and NEW not in durable_before
    if inherited and (profile / 'auth.json').exists():
        local = json.loads((profile / 'auth.json').read_text())
        assert not local.get('credential_pool', {}).get('anthropic')


def test_execute_preserves_primary_pool_but_not_for_an_explicit_key(tmp_path, monkeypatch):
    from tests.gateway.test_managed_worker_route_key import _execute
    from agent.credential_pool import CredentialPool
    _seed(tmp_path, monkeypatch)
    frame = _frame(tmp_path, monkeypatch)
    built = _execute(frame, monkeypatch)
    assert built['api_key'] == OLD
    assert isinstance(built['credential_pool'], CredentialPool)
    assert built['credential_pool'].provider == 'anthropic'
    explicit = _execute({**frame, 'api_key': 'fixture-launch-key'}, monkeypatch)
    assert explicit['api_key'] == 'fixture-launch-key'
    assert explicit.get('credential_pool') is None
