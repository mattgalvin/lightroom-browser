"""The Flask app: login/callback, server-side sessions, token refresh, error handling."""

import logging
import threading
import time
from unittest import mock

import pytest
import requests

import lightroom_client as lc


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f'HTTP {status}', response=response)


def start_login(browser):
    """Hit /oauth/start and return the state that was put in the session."""
    browser.get('/oauth/start')
    with browser.session() as s:
        return s['oauth_state']


# --- login and the callback ---------------------------------------------------

def test_authorization_url_carries_the_session_state(make_browser):
    b = make_browser()
    r = b.get('/oauth/start')
    with b.session() as s:
        state = s['oauth_state']
    assert r.status_code == 302 and f'state={state}' in r.headers['Location']


def test_each_login_gets_its_own_state(make_browser):
    assert start_login(make_browser()) != start_login(make_browser())


def test_callback_rejects_wrong_missing_and_replayed_state(make_browser, app_module):
    b = make_browser()
    state = start_login(b)
    assert b.get('/callback?code=x&state=WRONG').status_code == 400
    # the failed attempt used up the stored state, so even the right value is now rejected
    assert b.get(f'/callback?code=x&state={state}').status_code == 400

    b = make_browser()
    start_login(b)
    assert b.get('/callback?code=x').status_code == 400          # no state at all

    assert make_browser().get('/callback?code=x&state=anything').status_code == 400   # no login started


def test_state_from_another_browser_is_not_accepted(make_browser):
    attacker, victim = make_browser(), make_browser()
    attacker_state = start_login(attacker)
    start_login(victim)
    assert victim.get(f'/callback?code=x&state={attacker_state}').status_code == 400


def test_successful_callback_stores_tokens_server_side(make_browser, app_module):
    b = make_browser()
    state = start_login(b)
    huge = 'x' * 6000      # larger than a cookie could hold
    token_response = {'access_token': huge, 'refresh_token': huge + 'r', 'expires_in': 86399999}
    with mock.patch.object(app_module.oauth_handler, 'get_access_token', return_value=token_response) as exchange:
        r = b.get(f'/callback?code=abc&state={state}')
    exchange.assert_called_once_with('abc')
    assert r.status_code == 302 and r.headers['Location'].endswith('/albums')
    assert len(r.headers['Set-Cookie']) < 400
    with b.session() as s:
        assert set(s.keys()) == {'sid'}
        entry = app_module.token_store.get(s['sid'])
    assert entry.access_token == huge and entry.refresh_token == huge + 'r'
    assert 80000 < entry.expires_at - time.time() < 90000      # milliseconds were converted

    # the one-time state cannot be replayed
    assert b.get(f'/callback?code=abc&state={state}').status_code == 400


def test_failed_token_exchange_shows_a_generic_message(make_browser, app_module):
    b = make_browser()
    state = start_login(b)
    with mock.patch.object(app_module.oauth_handler, 'get_access_token', side_effect=Exception('secret details')):
        r = b.get(f'/callback?code=abc&state={state}')
    assert r.status_code == 500 and b'secret details' not in r.data


def test_adobe_error_text_is_not_reflected_into_the_page(make_browser):
    r = make_browser().get('/callback?error=x&error_description=<script>alert(1)</script>')
    assert r.status_code == 400 and b'<script>' not in r.data


def test_oauth_errors_cannot_forge_log_lines(make_browser, caplog):
    with caplog.at_level(logging.WARNING):
        make_browser().get('/callback?error=bad%0AFORGED&error_description=hello%0AFORGED%20LOG%20LINE' + '%41' * 5000)
    assert '\nFORGED' not in caplog.text
    assert 'hello\\nFORGED LOG LINE' in caplog.text
    assert max(len(line) for line in caplog.text.splitlines()) < 600     # truncated


# --- sessions ----------------------------------------------------------------

def test_logout_discards_tokens_and_the_cached_catalog(make_browser, app_module):
    from lightroom_client import LightroomClient
    b = make_browser(logged_in=True, access_token='LOGOUT-TOKEN')
    cache = app_module.lightroom_client._catalog_cache
    cache[LightroomClient._token_key('LOGOUT-TOKEN')] = {'id': 'c'}
    b.get('/logout')
    assert app_module.token_store.get(b.sid) is None
    assert LightroomClient._token_key('LOGOUT-TOKEN') not in cache
    with mock.patch.object(app_module.lightroom_client, 'get_albums_page', return_value=([], None)):
        assert b.get('/albums').status_code == 302


def test_stale_session_id_goes_to_login_without_a_redirect_loop(make_browser):
    b = make_browser(sid='stale-sid-from-before-a-restart')
    login = b.get('/login')
    assert login.status_code == 200 and b'Logout' not in login.data
    r = b.get('/albums')
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')


def test_nav_links_follow_the_login_state(make_browser, app_module):
    with mock.patch.object(app_module.lightroom_client, 'get_albums_page', return_value=([], None)):
        assert b'Logout' in make_browser(logged_in=True).get('/albums').data
    assert b'Logout' not in make_browser().get('/login').data


# --- token refresh ---------------------------------------------------------------

def test_expiring_token_is_refreshed_and_the_catalog_cache_follows(make_browser, app_module):
    from lightroom_client import LightroomClient
    b = make_browser(logged_in=True, access_token='CARRY-OLD', expires_in=5)   # inside the 60s margin
    cache = app_module.lightroom_client._catalog_cache
    cache[LightroomClient._token_key('CARRY-OLD')] = {'id': 'c'}
    refreshed = {'access_token': 'CARRY-NEW', 'expires_in': 3600}               # no new refresh token
    with mock.patch.object(app_module.oauth_handler, 'refresh_access_token', return_value=refreshed) as refresh, \
         mock.patch.object(app_module.lightroom_client, 'get_asset_rendition', return_value=b'img') as rendition:
        assert b.get('/thumbnail/abc').status_code == 200
    refresh.assert_called_once_with('R1')
    assert rendition.call_args[0][0] == 'CARRY-NEW'
    assert app_module.token_store.get(b.sid).refresh_token == 'R1'              # old refresh token kept
    assert LightroomClient._token_key('CARRY-NEW') in cache
    assert LightroomClient._token_key('CARRY-OLD') not in cache                 # moved, not duplicated


def test_valid_token_is_not_refreshed(make_browser, app_module):
    b = make_browser(logged_in=True)
    with mock.patch.object(app_module.oauth_handler, 'refresh_access_token') as refresh, \
         mock.patch.object(app_module.lightroom_client, 'get_asset_rendition', return_value=b'img'):
        assert b.get('/thumbnail/abc').status_code == 200
    refresh.assert_not_called()


def test_parallel_requests_share_one_refresh(make_browser, app_module):
    first = make_browser(logged_in=True, expires_in=5)
    calls = []

    def slow_refresh(refresh_token):
        calls.append(refresh_token)
        time.sleep(0.3)
        return {'access_token': 'A2', 'refresh_token': 'R2', 'expires_in': 3600}   # rotated

    statuses, used = [], []

    def hit():
        statuses.append(make_browser(sid=first.sid).get('/thumbnail/abc').status_code)

    def rendition(token, asset_id, rendition_type=None):
        used.append(token)
        return b'img'

    with mock.patch.object(app_module.oauth_handler, 'refresh_access_token', side_effect=slow_refresh), \
         mock.patch.object(app_module.lightroom_client, 'get_asset_rendition', side_effect=rendition):
        threads = [threading.Thread(target=hit) for _ in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]

    assert calls == ['R1']                      # a single refresh, not twenty racing ones
    assert statuses == [200] * 20               # and nobody was logged out
    assert set(used) == {'A2'}
    assert app_module.token_store.get(first.sid).refresh_token == 'R2'


@pytest.mark.parametrize('failure', [
    requests.ConnectionError('down'),
    requests.Timeout('slow'),
    http_error(500), http_error(502), http_error(503), http_error(429),
])
def test_transient_refresh_failures_keep_the_session(make_browser, app_module, failure):
    b = make_browser(logged_in=True, expires_in=5)
    with mock.patch.object(app_module.oauth_handler, 'refresh_access_token', side_effect=failure):
        image = b.get('/thumbnail/abc')
        api = b.get('/api/albums?name_after=x')
        page = b.get('/albums')
    for r in (image, api, page):
        assert r.status_code == 503 and r.headers['Retry-After'] == '5'
    assert api.get_json()['error']
    entry = app_module.token_store.get(b.sid)
    assert entry is not None and entry.refresh_token == 'R1'      # the refresh token is not thrown away

    # Adobe recovers: the very next request just works
    with mock.patch.object(app_module.oauth_handler, 'refresh_access_token',
                           return_value={'access_token': 'A9', 'expires_in': 3600}), \
         mock.patch.object(app_module.lightroom_client, 'get_asset_rendition', return_value=b'img'):
        assert b.get('/thumbnail/abc').status_code == 200


@pytest.mark.parametrize('status', [400, 401])
def test_definitive_refresh_rejection_ends_the_session(make_browser, app_module, status):
    b = make_browser(logged_in=True, expires_in=5)
    with mock.patch.object(app_module.oauth_handler, 'refresh_access_token', side_effect=http_error(status)):
        r = b.get('/albums')
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')
    assert app_module.token_store.get(b.sid) is None


def test_expired_token_without_a_refresh_token_ends_the_session(make_browser, app_module):
    b = make_browser(logged_in=True, expires_in=5, refresh_token=None)
    r = b.get('/albums')
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')
    assert app_module.token_store.get(b.sid) is None


def test_token_rejected_by_the_lightroom_api_ends_the_session(make_browser, app_module):
    b = make_browser(logged_in=True)
    with mock.patch.object(app_module.lightroom_client, 'get_asset_rendition', side_effect=lc.TokenExpiredError('x')):
        assert b.get('/thumbnail/abc').status_code == 401
    assert app_module.token_store.get(b.sid) is None


# --- error handling and response headers ---------------------------------------------

def test_unexpected_errors_are_generic_in_the_browser(make_browser, app_module):
    b = make_browser(logged_in=True)
    with mock.patch.object(app_module.lightroom_client, 'get_albums_page', side_effect=RuntimeError('secret internals')):
        page = b.get('/albums')
    assert page.status_code == 500 and b'secret internals' not in page.data
    with mock.patch.object(app_module.lightroom_client, 'get_albums_page', side_effect=RuntimeError('secret internals')):
        api = b.get('/api/albums?name_after=x')
    assert api.status_code == 500 and 'secret internals' not in api.get_data(as_text=True)


def test_error_log_does_not_let_the_url_forge_lines(make_browser, app_module, caplog):
    b = make_browser(logged_in=True)
    with caplog.at_level(logging.ERROR), \
         mock.patch.object(app_module.lightroom_client, 'get_asset_rendition', side_effect=RuntimeError('boom')):
        b.get('/thumbnail/x%0AFORGED')
    assert '\nFORGED' not in caplog.text


def test_invalid_ids_are_a_400(make_browser, app_module):
    b = make_browser(logged_in=True)
    # rejected before any Adobe call is made (the no_real_network fixture would fail the test otherwise)
    assert b.get('/api/album/abc/photos?page_url=https://evil.example/x').status_code == 400
    assert b.get('/api/album/abc%0A/photos?page_url=https://lr.adobe.io/v2/x').status_code == 400
    assert b.get('/api/album/abc/photos?page_url=https://lr.adobe.io/v2/x%0AINJECT').status_code == 400
    assert b.get('/album/abc%0A').status_code == 400


@pytest.mark.parametrize('path, target', [
    ('/api/album/abc/cover', 'get_album_first_asset'),
    ('/thumbnail/abc', 'get_asset_rendition'),
])
def test_adobe_rate_limit_becomes_a_429_with_capped_retry_after(make_browser, app_module, path, target):
    b = make_browser(logged_in=True)
    with mock.patch.object(app_module.lightroom_client, target, side_effect=lc.RateLimitedError(120)):
        r = b.get(path)
    assert r.status_code == 429 and r.headers['Retry-After'] == '30'


def test_image_cache_headers(make_browser, app_module):
    b = make_browser(logged_in=True)
    with mock.patch.object(app_module.lightroom_client, 'get_asset_rendition', return_value=b'img'), \
         mock.patch.object(app_module.lightroom_client, 'get_album_first_asset', return_value='asset1'):
        thumb = b.get('/thumbnail/abc')
        cover = b.get('/api/album/abc/cover')
    assert thumb.headers['Cache-Control'] == 'private, max-age=3600'
    assert cover.headers['Cache-Control'] == 'private, max-age=600'
    with mock.patch.object(app_module.lightroom_client, 'get_album_first_asset', return_value=None):
        assert b.get('/api/album/abc/cover').status_code == 404


def test_albums_page_loads_covers_lazily_and_escapes_names(make_browser, app_module):
    b = make_browser(logged_in=True)
    albums = [
        {'id': 'a1', 'payload': {'name': 'Tom & "Jerry" <b>', 'assetCount': 3}},
        {'id': 'a2', 'payload': {'name': 'Empty', 'assetCount': 0}},
    ]
    with mock.patch.object(app_module.lightroom_client, 'get_albums_page', return_value=(albums, 'Next&Name')) as page:
        html = b.get('/albums').data.decode()
    page.assert_called_once()                                    # no per-album Adobe calls while rendering
    assert 'data-cover-src="/api/album/a1/cover"' in html
    assert '/api/album/a2/cover' not in html                     # known-empty album gets no cover request
    assert 'Tom &amp; &#34;Jerry&#34; &lt;b&gt;' in html
    assert 'let nextNameAfter = "Next\\u0026Name"' in html


def test_gallery_escapes_hostile_filenames(make_browser, app_module):
    b = make_browser(logged_in=True)
    evil = "x');alert(1);//</script><img src=x onerror=alert(2)>.jpg"
    photos = [{'asset': {'id': 'p1', 'payload': {'importSource': {'fileName': evil}}}}]
    with mock.patch.object(app_module.lightroom_client, 'get_album', return_value={'payload': {'name': 'A', 'assetCount': 1}}), \
         mock.patch.object(app_module.lightroom_client, 'get_album_assets_page',
                           return_value=(photos, 'https://lr.adobe.io/v2/next', None)):
        r = b.get('/album/alb1')
    html = r.data.decode()
    assert r.status_code == 200
    assert '</script><img' not in html and 'onerror=alert(2)>' not in html
    assert 'onclick="openLightbox' not in html
