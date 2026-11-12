"""Shared test setup. The tests never contact Adobe and never use real credentials."""

import os
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set before `app` is imported. load_dotenv() does not override variables that are already
# set, so a developer's real .env can't leak into the tests.
os.environ.setdefault('ADOBE_CLIENT_ID', 'test-client-id')
os.environ.setdefault('ADOBE_CLIENT_SECRET', 'test-client-secret')

HTTPS = 'https://localhost'  # session cookies are Secure, so the test client must use https


class Browser:
    """A Flask test client that always talks https://localhost."""

    def __init__(self, flask_client, sid=None):
        self.client = flask_client
        self.sid = sid

    def get(self, path, **kwargs):
        return self.client.get(path, base_url=HTTPS, **kwargs)

    def session(self):
        return self.client.session_transaction(base_url=HTTPS)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail loudly if a test would reach the network instead of using a mock."""
    import requests.adapters

    def refuse(self, request, **kwargs):
        raise AssertionError(f"test tried to contact the network: {request.method} {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', refuse)


@pytest.fixture
def app_module():
    import app
    return app


@pytest.fixture
def make_browser(app_module):
    """make_browser() -> logged-out browser; make_browser(logged_in=True, ...) -> logged in.

    Extra keyword arguments override the stored token response, e.g. expires_in=5 to get a
    token that is about to expire. Pass sid=... to share an existing login.
    """
    def make(logged_in=False, sid=None, **token_overrides):
        if logged_in and sid is None:
            token_data = {'access_token': 'A1', 'refresh_token': 'R1', 'expires_in': 3600}
            token_data.update(token_overrides)
            sid = app_module.token_store.create(token_data)
        browser = Browser(app_module.app.test_client(), sid=sid)
        if sid:
            with browser.session() as s:
                s['sid'] = sid
        return browser
    return make


@pytest.fixture
def lr_client():
    """A fresh LightroomClient (own caches), not the app's shared one."""
    from lightroom_client import LightroomClient
    from oauth_handler import OAuthHandler
    return LightroomClient(OAuthHandler('test-client-id', 'test-client-secret', 'https://localhost/callback'))
