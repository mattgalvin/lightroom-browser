"""Pure helpers: ID/URL validation, token expiry handling, the token store."""

import pytest

import token_store
import lightroom_client as lc
from token_store import TokenStore, SessionEntry, expires_at_from


# --- _check_id -------------------------------------------------------------

@pytest.mark.parametrize('value', ['abc', 'A1_b-2', '0' * 32])
def test_check_id_accepts_plain_ids(value):
    assert lc._check_id(value) == value


@pytest.mark.parametrize('value', [
    '', 'abc\n', '\nabc', 'abc\r\n', 'a b', 'a/b', '..', '../x', 'a.b', 'abc?x=1',
    '٣',  # a non-ASCII digit
    None, 5,
])
def test_check_id_rejects_everything_else(value):
    # "abc\n" is the case a `$`-anchored regex lets through
    with pytest.raises(lc.InvalidInputError):
        lc._check_id(value)


# --- _validate_api_url ------------------------------------------------------

@pytest.mark.parametrize('url', [
    'https://lr.adobe.io/v2/catalogs/c/albums',
    'https://lr.adobe.io/v2/x?limit=1&offset=20',
])
def test_validate_api_url_accepts_the_api_host(lr_client, url):
    assert lr_client._validate_api_url(url) == url


@pytest.mark.parametrize('url', [
    'http://lr.adobe.io/v2/x',                  # not https
    'https://evil.example/x',                   # other host
    'https://lr.adobe.io.evil.com/x',           # look-alike host
    'https://lr.adobe.io@evil.com/x',           # userinfo trick
    'https://evil.com/?https://lr.adobe.io/',
    '//lr.adobe.io/v2/x',                       # scheme-less
    '/v2/x',                                    # relative
    'https://lr.adobe.io/v2/x\nINJECT',         # control characters
    'https://lr.adobe.io/v2/x\r\nX-Evil: 1',
])
def test_validate_api_url_rejects_everything_else(lr_client, url):
    with pytest.raises(lc.InvalidInputError):
        lr_client._validate_api_url(url)


# --- expiry handling (seconds vs milliseconds) -------------------------------

def test_expires_in_seconds():
    assert expires_at_from({'expires_in': 3600}, now=1000) == 4600


def test_expires_in_milliseconds_is_detected():
    # Adobe IMS reports roughly 24h as 86399999 (milliseconds)
    assert expires_at_from({'expires_in': 86399999}, now=0) == pytest.approx(86399.999)


def test_expires_in_accepts_strings_and_defaults_to_an_hour():
    assert expires_at_from({'expires_in': '7200'}, now=0) == 7200
    assert expires_at_from({}, now=500) == 4100


# --- SessionEntry ------------------------------------------------------------

def test_entry_update_keeps_old_refresh_token_when_none_is_returned():
    entry = SessionEntry({'access_token': 'A1', 'refresh_token': 'R1', 'expires_in': 10})
    entry.update({'access_token': 'A2', 'expires_in': 3600})
    assert (entry.access_token, entry.refresh_token) == ('A2', 'R1')
    assert not entry.needs_refresh()


def test_entry_update_uses_rotated_refresh_token():
    entry = SessionEntry({'access_token': 'A1', 'refresh_token': 'R1', 'expires_in': 10})
    entry.update({'access_token': 'A2', 'refresh_token': 'R2', 'expires_in': 3600})
    assert entry.refresh_token == 'R2'


def test_entry_update_changes_nothing_if_the_response_is_malformed():
    entry = SessionEntry({'access_token': 'A1', 'refresh_token': 'R1', 'expires_in': 3600})
    before = entry.expires_at
    with pytest.raises(KeyError):
        entry.update({'refresh_token': 'R2', 'expires_in': 1})
    assert (entry.access_token, entry.refresh_token, entry.expires_at) == ('A1', 'R1', before)


def test_needs_refresh_uses_the_margin():
    entry = SessionEntry({'access_token': 'A', 'expires_in': 100})
    assert not entry.needs_refresh(margin=10)
    assert entry.needs_refresh(margin=200)


# --- TokenStore --------------------------------------------------------------

def test_store_create_get_delete():
    store = TokenStore()
    sid = store.create({'access_token': 'A', 'refresh_token': 'R', 'expires_in': 3600})
    assert len(sid) >= 32 and store.get(sid).access_token == 'A'
    store.delete(sid)
    assert store.get(sid) is None
    store.delete(sid)       # deleting twice is fine
    store.delete(None)
    assert store.get(None) is None and store.get('nope') is None


def test_store_session_ids_are_unique():
    store = TokenStore()
    sids = {store.create({'access_token': 'A'}) for _ in range(50)}
    assert len(sids) == 50


def test_store_purges_idle_sessions(monkeypatch):
    store = TokenStore(idle_ttl=10)
    monkeypatch.setattr(token_store.time, 'time', lambda: 1000.0)
    old = store.create({'access_token': 'A'})
    monkeypatch.setattr(token_store.time, 'time', lambda: 1011.0)
    fresh = store.create({'access_token': 'B'})      # creating a session sweeps idle ones
    assert store.get(old) is None
    assert store.get(fresh) is not None


def test_bind_host_refuses_debugger_on_the_network(app_module):
    with pytest.raises(SystemExit):
        app_module.resolve_bind_host('0.0.0.0', debug=True)
    assert app_module.resolve_bind_host('127.0.0.1', debug=True) == '127.0.0.1'
    assert app_module.resolve_bind_host('localhost', debug=True) == 'localhost'
    assert app_module.resolve_bind_host('0.0.0.0', debug=False) == '0.0.0.0'


def test_log_safe_truncates_and_escapes_newlines(app_module):
    rendered = app_module.log_safe('a\nFORGED LOG LINE' + 'x' * 5000)
    assert '\n' not in rendered and len(rendered) < 300
    assert app_module.log_safe(None) == "''"
