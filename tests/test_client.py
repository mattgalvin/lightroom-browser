"""LightroomClient: redirects, rate limits, caching and the concurrency cap (no network)."""

import threading
import time

import pytest
import requests

import lightroom_client as lc


class FakeResponse:
    def __init__(self, status=200, headers=None, body='{}', content=b'img'):
        self.status_code = status
        self.headers = headers or {}
        self.text = body
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}')


@pytest.fixture
def sent(monkeypatch):
    """Capture every outgoing request; set `sent.responses` to script the replies."""
    class Sent:
        calls = []
        responses = []
    s = Sent()
    s.calls = []

    def fake_request(method, url, **kwargs):
        s.calls.append({'method': method, 'url': url, **kwargs})
        i = min(len(s.calls) - 1, len(s.responses) - 1)
        return s.responses[i]

    monkeypatch.setattr(lc.requests, 'request', fake_request)
    return s


@pytest.fixture
def client(lr_client):
    lr_client.get_catalog = lambda token: {'id': 'cat'}
    return lr_client


# --- redirects ---------------------------------------------------------------

def test_requests_never_follow_redirects_on_their_own(client, sent):
    sent.responses = [FakeResponse(200, body='{"resources": []}')]
    client.get_album('T', 'abc')
    assert sent.calls[0]['allow_redirects'] is False
    assert sent.calls[0]['timeout'] == client.REQUEST_TIMEOUT


def test_same_host_redirect_is_followed_with_credentials(client, sent):
    sent.responses = [
        FakeResponse(302, {'Location': '/v2/catalogs/cat/albums/moved'}),
        FakeResponse(200, body='{"id": "moved"}'),
    ]
    assert client.get_album('T', 'abc') == {'id': 'moved'}
    assert [c['url'] for c in sent.calls] == [
        'https://lr.adobe.io/v2/catalogs/cat/albums/abc',
        'https://lr.adobe.io/v2/catalogs/cat/albums/moved',
    ]
    assert all(c['headers']['Authorization'] == 'Bearer T' for c in sent.calls)


@pytest.mark.parametrize('location', [
    'https://cdn.example.com/file.jpg',        # other host
    'http://lr.adobe.io/v2/x',                 # https -> http downgrade
    'https://lr.adobe.io.evil.com/v2/x',
])
def test_redirect_off_the_api_host_is_refused_and_not_sent(client, sent, location):
    sent.responses = [FakeResponse(302, {'Location': location}), FakeResponse(200)]
    with pytest.raises(lc.UnexpectedRedirectError):
        client.get_album('T', 'abc')
    assert len(sent.calls) == 1          # credentials were only ever sent to lr.adobe.io


def test_renditions_get_the_same_redirect_protection(client, sent):
    sent.responses = [FakeResponse(307, {'Location': 'https://cdn.example.com/x.jpg'})]
    with pytest.raises(lc.UnexpectedRedirectError):
        client.get_asset_rendition('T', 'asset1', 'thumbnail2x')
    assert len(sent.calls) == 1


def test_redirect_without_location_and_redirect_loops_fail(client, sent):
    sent.responses = [FakeResponse(302)]
    with pytest.raises(lc.UnexpectedRedirectError):
        client.get_album('T', 'abc')

    sent.calls.clear()
    sent.responses = [FakeResponse(302, {'Location': '/v2/loop'})]
    with pytest.raises(lc.UnexpectedRedirectError):
        client.get_album('T', 'abc')
    assert len(sent.calls) == client.MAX_REDIRECTS + 1


# --- error mapping -----------------------------------------------------------

def test_401_means_token_expired(client, sent):
    sent.responses = [FakeResponse(401)]
    with pytest.raises(lc.TokenExpiredError):
        client.get_album('T', 'abc')


def test_429_carries_retry_after(client, sent):
    sent.responses = [FakeResponse(429, {'Retry-After': '7'})]
    with pytest.raises(lc.RateLimitedError) as info:
        client.get_album('T', 'abc')
    assert info.value.retry_after == 7


def test_429_with_unusable_retry_after_falls_back_to_five_seconds(client, sent):
    sent.responses = [FakeResponse(429, {'Retry-After': 'soon'})]
    with pytest.raises(lc.RateLimitedError) as info:
        client.get_asset_rendition('T', 'asset1')
    assert info.value.retry_after == 5


def test_untrusted_ids_and_page_urls_never_reach_the_network(client, sent):
    sent.responses = [FakeResponse(200)]
    with pytest.raises(lc.InvalidInputError):
        client.get_album('T', 'abc\n')
    with pytest.raises(lc.InvalidInputError):
        client.get_asset_rendition('T', 'asset1', 'thumb\nnail')
    with pytest.raises(lc.InvalidInputError):
        client.get_album_assets_page('T', 'abc', page_url='https://evil.example/x')
    with pytest.raises(lc.InvalidInputError):
        client.get_album_assets_page('T', 'abc', page_url='https://lr.adobe.io/v2/x\nINJECT')
    assert sent.calls == []


# --- URL encoding -------------------------------------------------------------

def test_name_after_is_url_encoded(client, sent):
    sent.responses = [FakeResponse(200, body='{"resources": [], "links": {}}')]
    client.get_albums_page('T', limit=5, name_after='Tom & Jerry #1')
    assert sent.calls[0]['url'] == (
        'https://lr.adobe.io/v2/catalogs/cat/albums?limit=5&name_after=Tom+%26+Jerry+%231'
    )


def test_next_name_after_is_decoded_from_the_next_link(client, sent):
    body = ('{"resources": [], "base": "https://lr.adobe.io/v2/", '
            '"links": {"next": {"href": "catalogs/cat/albums?limit=5&name_after=Tom+%26+Jerry"}}}')
    sent.responses = [FakeResponse(200, body=body)]
    _, next_name_after = client.get_albums_page('T', limit=5)
    assert next_name_after == 'Tom & Jerry'


# --- catalog cache --------------------------------------------------------------

@pytest.fixture
def catalog_calls(lr_client, monkeypatch):
    calls = []

    def fake(token, method, endpoint, **kwargs):
        calls.append(token)
        return {'id': 'cat-' + token}

    monkeypatch.setattr(lr_client, '_make_request', fake)
    return calls


def test_catalog_is_cached_per_token(lr_client, catalog_calls):
    assert lr_client.get_catalog('U1')['id'] == 'cat-U1'
    assert lr_client.get_catalog('U2')['id'] == 'cat-U2'     # another user is not served U1's catalog
    lr_client.get_catalog('U1')
    lr_client.get_catalog('U2')
    assert catalog_calls == ['U1', 'U2']


def test_catalog_survives_a_token_refresh(lr_client, catalog_calls):
    lr_client.get_catalog('OLD')
    lr_client.carry_over_catalog('OLD', 'NEW')
    assert lr_client.get_catalog('NEW')['id'] == 'cat-OLD'
    assert catalog_calls == ['OLD']                          # no refetch after the refresh
    lr_client.carry_over_catalog('never-seen', 'other')      # harmless no-op


def test_forget_catalog(lr_client, catalog_calls):
    lr_client.get_catalog('U1')
    lr_client.forget_catalog('U1')
    lr_client.get_catalog('U1')
    assert catalog_calls == ['U1', 'U1']


def test_catalog_cache_is_bounded(lr_client, catalog_calls):
    lr_client.CATALOG_CACHE_SIZE = 3
    for i in range(10):
        lr_client.get_catalog(f'U{i}')
    assert len(lr_client._catalog_cache) == 3


# --- first-asset (cover) cache ---------------------------------------------------

@pytest.fixture
def asset_calls(lr_client, monkeypatch):
    calls = []
    lr_client.get_catalog = lambda token: {'id': 'cat-' + token}

    def fake(token, method, endpoint, **kwargs):
        calls.append((token, endpoint))
        if '/albums/empty/' in endpoint:
            return {'resources': []}
        album = endpoint.split('/albums/')[1].split('/')[0]
        return {'resources': [{'asset': {'id': 'asset-' + album}}]}

    monkeypatch.setattr(lr_client, '_make_request', fake)
    return calls


def test_first_asset_is_fetched_once_per_album(lr_client, asset_calls):
    for _ in range(5):
        assert lr_client.get_album_first_asset('U1', 'alb1') == 'asset-alb1'
    assert len(asset_calls) == 1


def test_first_asset_cache_is_per_catalog(lr_client, asset_calls):
    lr_client.get_album_first_asset('U1', 'alb1')
    lr_client.get_album_first_asset('U2', 'alb1')
    assert len(asset_calls) == 2


def test_empty_albums_are_cached_too(lr_client, asset_calls):
    for _ in range(3):
        assert lr_client.get_album_first_asset('U1', 'empty') is None
    assert len(asset_calls) == 1


def test_first_asset_cache_expires(lr_client, asset_calls, monkeypatch):
    lr_client.get_album_first_asset('U1', 'alb1')
    real = time.monotonic
    monkeypatch.setattr(lc.time, 'monotonic', lambda: real() + lr_client.FIRST_ASSET_TTL + 1)
    lr_client.get_album_first_asset('U1', 'alb1')
    assert len(asset_calls) == 2


def test_first_asset_cache_is_bounded(lr_client, asset_calls):
    lr_client.FIRST_ASSET_CACHE_SIZE = 3
    for i in range(10):
        lr_client.get_album_first_asset('U1', f'x{i}')
    assert len(lr_client._first_asset_cache) == 3


# --- concurrency cap ------------------------------------------------------------

def test_calls_to_adobe_are_capped(monkeypatch):
    from oauth_handler import OAuthHandler
    capped = lc.LightroomClient(OAuthHandler('id', 'secret', 'https://localhost/cb'), max_concurrent_requests=4)
    capped.get_catalog = lambda token: {'id': 'cat'}
    state = {'live': 0, 'peak': 0}
    lock = threading.Lock()

    def slow_request(method, url, **kwargs):
        with lock:
            state['live'] += 1
            state['peak'] = max(state['peak'], state['live'])
        time.sleep(0.05)
        with lock:
            state['live'] -= 1
        return FakeResponse(200, body='{"resources": []}')

    monkeypatch.setattr(lc.requests, 'request', slow_request)
    targets = [capped.get_album if i % 2 else capped.get_asset_rendition for i in range(24)]
    threads = [threading.Thread(target=t, args=('T', 'abc')) for t in targets]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert 1 < state['peak'] <= 4
