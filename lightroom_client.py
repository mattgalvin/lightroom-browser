"""
Lightroom API Client
Handles API calls to Adobe Lightroom API
"""

import re
import json
import hashlib
import time
import logging
import threading
from collections import OrderedDict
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests

# Set up logger
logger = logging.getLogger(__name__)


class TokenExpiredError(Exception):
    """Raised when the Lightroom API rejects the access token (HTTP 401)."""


class InvalidInputError(Exception):
    """Raised when a caller-supplied ID or URL is not acceptable."""


class RateLimitedError(Exception):
    """Raised when the Lightroom API answers HTTP 429. `retry_after` is in seconds."""

    def __init__(self, retry_after=5):
        super().__init__(f"Lightroom API rate limit hit (retry after {retry_after}s)")
        self.retry_after = retry_after


_ID_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _check_id(value, what='id'):
    """Make sure an ID is safe to place in an API URL path."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise InvalidInputError(f"Invalid {what}")
    return value


class LightroomClient:
    """Client for interacting with Adobe Lightroom API"""

    # Lightroom API base URL
    API_BASE_URL = "https://lr.adobe.io/v2"
    API_HOST = urlparse(API_BASE_URL).netloc

    REQUEST_TIMEOUT = 30
    CATALOG_CACHE_SIZE = 256

    # An album's first asset (used for its cover) rarely changes, so remember it
    FIRST_ASSET_TTL = 600  # seconds
    FIRST_ASSET_CACHE_SIZE = 4096

    def __init__(self, oauth_handler, max_concurrent_requests=4):
        """
        Initialize Lightroom client

        Args:
            oauth_handler: OAuthHandler instance for authentication
            max_concurrent_requests: cap on simultaneous calls to Adobe, so a page full of
                                     covers/thumbnails can't trip Adobe's rate limits
        """
        self.oauth_handler = oauth_handler
        # Catalogs are per user, so cache them per access token (LRU-bounded)
        self._catalog_cache = OrderedDict()
        self._catalog_lock = threading.Lock()
        # (catalog_id, album_id) -> (expires_at, first_asset_id or None); LRU-bounded
        self._first_asset_cache = OrderedDict()
        self._first_asset_lock = threading.Lock()
        self._api_slots = threading.BoundedSemaphore(max_concurrent_requests)

    @staticmethod
    def _raise_if_rate_limited(response):
        if response.status_code == 429:
            try:
                retry_after = int(response.headers.get('Retry-After', 5))
            except (TypeError, ValueError):
                retry_after = 5
            logger.warning(f"Lightroom API rate limit hit (Retry-After {retry_after}s)")
            raise RateLimitedError(retry_after)

    def _process_json_response(self, response_text):
        """
        Process JSON response by stripping the while(1){} prefix

        Adobe Lightroom API responses are prepended with while(1){} to mitigate abuse.
        This must be stripped before parsing JSON.

        Args:
            response_text: Raw response text from API

        Returns:
            dict: Parsed JSON object
        """
        if not response_text:
            return None

        # Strip the while(1){} prefix using regex pattern from Adobe documentation
        # Pattern matches: while(1){} with optional whitespace
        while1_regex = re.compile(r'^while\s*\(\s*1\s*\)\s*{\s*}\s*', re.IGNORECASE)
        cleaned_text = while1_regex.sub('', response_text)

        return json.loads(cleaned_text)

    def _get_headers(self, access_token):
        """Get headers for API requests"""
        return {
            'Authorization': f'Bearer {access_token}',
            # Adobe requires x-api-key to be the client ID
            'x-api-key': self.oauth_handler.client_id,
            'Content-Type': 'application/json'
        }

    def _validate_api_url(self, url):
        """Only allow absolute URLs that point at the Lightroom API host."""
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.netloc != self.API_HOST:
            raise InvalidInputError("URL does not point to the Lightroom API")
        return url

    def _make_request(self, access_token, method, endpoint, **kwargs):
        """
        Make API request with error handling

        Args:
            access_token: OAuth access token
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path or absolute URL
            **kwargs: Additional arguments for requests

        Returns:
            dict: JSON response
        """
        # Support both relative endpoints and absolute URLs, per Adobe links/base docs:
        # https://developer.adobe.com/lightroom/lightroom-api-docs/guides/links_and_pagination/
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            url = self._validate_api_url(endpoint)
        else:
            url = f"{self.API_BASE_URL}{endpoint}"

        headers = self._get_headers(access_token)

        # Log request details (mask sensitive authorization header)
        safe_headers = headers.copy()
        if 'Authorization' in safe_headers:
            safe_headers['Authorization'] = 'Bearer ***REDACTED***'
        logger.info(f"Lightroom API Request: {method} {url}")
        logger.debug(f"Request headers: {safe_headers}")
        if kwargs:
            logger.debug(f"Request kwargs: {kwargs}")

        with self._api_slots:
            response = requests.request(
                method, url, headers=headers, verify=False, timeout=self.REQUEST_TIMEOUT, **kwargs
            )

        # Log response details
        logger.info(f"Lightroom API Response: {response.status_code} from {method} {url}")
        logger.debug(f"Response headers: {dict(response.headers)}")
        logger.debug(f"Response body length: {len(response.text)} characters")

        # Handle token expiration
        if response.status_code == 401:
            logger.error(f"Access token rejected for request to {url}")
            raise TokenExpiredError("Access token expired. Please re-authenticate.")

        self._raise_if_rate_limited(response)
        response.raise_for_status()

        # Process JSON response to strip while(1){} prefix
        # Adobe Lightroom API prepends this to all JSON responses
        resp = self._process_json_response(response.text)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Response body: \n{json.dumps(resp, indent=4)}")
        return resp

    def _get_paged_resources(self, access_token, initial_endpoint):
        """
        Helper to retrieve all paged resources following links.next.

        Uses the 'base' and 'links.next.href' fields as described in:
        https://developer.adobe.com/lightroom/lightroom-api-docs/guides/links_and_pagination/
        """
        resources = []
        page = self._make_request(access_token, 'GET', initial_endpoint)
        if not page:
            return resources

        resources.extend(page.get('resources', []))

        # Follow pagination using links.next
        while page.get('links', {}).get('next'):
            next_href = page['links']['next'].get('href')
            if not next_href:
                break

            base = page.get('base', self.API_BASE_URL)
            next_url = urljoin(base, next_href)

            page = self._make_request(access_token, 'GET', next_url)
            if not page:
                break

            resources.extend(page.get('resources', []))

        return resources

    def get_catalog(self, access_token):
        """
        Get the user's catalog information (cached per access token)

        Args:
            access_token: OAuth access token

        Returns:
            dict: Catalog information
        """
        key = hashlib.sha256(access_token.encode('utf-8')).hexdigest()

        with self._catalog_lock:
            catalog = self._catalog_cache.get(key)
            if catalog is not None:
                self._catalog_cache.move_to_end(key)
                return catalog

        catalog = self._make_request(access_token, 'GET', '/catalog')

        if catalog:
            with self._catalog_lock:
                self._catalog_cache[key] = catalog
                self._catalog_cache.move_to_end(key)
                while len(self._catalog_cache) > self.CATALOG_CACHE_SIZE:
                    self._catalog_cache.popitem(last=False)

        return catalog

    def _get_catalog_id(self, access_token):
        catalog = self.get_catalog(access_token)
        if not catalog:
            raise Exception("Could not retrieve catalog")
        catalog_id = catalog.get('id')
        if not catalog_id:
            raise Exception("Could not retrieve catalog ID")
        return catalog_id

    def get_albums_page(self, access_token, limit=20, name_after=None):
        """
        Get a single page of albums using cursor-based pagination

        Args:
            access_token: OAuth access token
            limit: Number of albums per page
            name_after: Cursor for pagination (album name to start after)

        Returns:
            tuple: (albums_list, next_name_after)
                - albums_list: List of album resources
                - next_name_after: Value for name_after to get next page, or None if no more pages
        """
        catalog_id = self._get_catalog_id(access_token)

        # urlencode so album names with &, #, spaces, etc. survive intact
        params = {'limit': int(limit)}
        if name_after:
            params['name_after'] = name_after
        endpoint = f'/catalogs/{catalog_id}/albums?{urlencode(params)}'

        page = self._make_request(access_token, 'GET', endpoint)

        if not page:
            return [], None

        resources = page.get('resources', [])
        links = page.get('links', {})
        base = page.get('base', self.API_BASE_URL)

        # Extract name_after from the next link if it exists
        next_name_after = None
        if links.get('next') and links['next'].get('href'):
            next_href = links['next']['href']
            # Parse the next link to extract name_after parameter (parse_qs decodes it)
            parsed = urlparse(urljoin(base, next_href))
            qs = parse_qs(parsed.query)
            if 'name_after' in qs and qs['name_after']:
                next_name_after = qs['name_after'][0]

        return resources, next_name_after

    def get_album(self, access_token, album_id):
        """
        Get specific album information

        Args:
            access_token: OAuth access token
            album_id: Album ID

        Returns:
            dict: Album information
        """
        _check_id(album_id, 'album id')
        catalog_id = self._get_catalog_id(access_token)
        return self._make_request(access_token, 'GET', f'/catalogs/{catalog_id}/albums/{album_id}')

    def get_album_assets_page(self, access_token, album_id, limit=20, page_url=None):
        """
        Get a single page of assets for an album with limit and pagination links.
        """
        _check_id(album_id, 'album id')
        catalog_id = self._get_catalog_id(access_token)

        if page_url:
            # Raises InvalidInputError unless this is an https://lr.adobe.io URL
            self._validate_api_url(page_url)
            page = self._make_request(access_token, 'GET', page_url)
        else:
            endpoint = f'/catalogs/{catalog_id}/albums/{album_id}/assets?limit={int(limit)}'
            page = self._make_request(access_token, 'GET', endpoint)

        if not page:
            return [], None, None

        resources = page.get('resources', [])
        links = page.get('links', {})
        base = page.get('base', self.API_BASE_URL)

        next_url = None
        prev_url = None

        if links.get('next') and links['next'].get('href'):
            next_url = urljoin(base, links['next']['href'])

        # Adobe pagination may not provide a prev link; compute it from the current page_url when possible
        if links.get('prev') and links['prev'].get('href'):
            prev_url = urljoin(base, links['prev']['href'])
        elif page_url:
            parsed = urlparse(page_url)
            qs = parse_qs(parsed.query)
            try:
                current_limit = int(qs.get('limit', [limit])[0])
                current_offset = int(qs.get('offset', [0])[0])
            except ValueError:
                current_limit = int(limit)
                current_offset = 0

            if current_offset > 0:
                new_offset = max(0, current_offset - current_limit)
                qs['offset'] = [str(new_offset)]
                qs['limit'] = [str(current_limit)]
                new_query = urlencode(qs, doseq=True)
                prev_url = urlunparse(parsed._replace(query=new_query))

        return resources, next_url, prev_url

    def get_album_first_asset(self, access_token, album_id):
        """
        Get the first asset from an album

        Args:
            access_token: OAuth access token
            album_id: Album ID

        Returns:
            str: First asset ID or None if album is empty
        """
        _check_id(album_id, 'album id')
        catalog_id = self._get_catalog_id(access_token)

        # Catalogs are per user, so this key is safe to share across requests
        key = (catalog_id, album_id)
        with self._first_asset_lock:
            cached = self._first_asset_cache.get(key)
            if cached is not None and cached[0] > time.monotonic():
                self._first_asset_cache.move_to_end(key)
                return cached[1]

        # Fetch just the first asset (limit=1)
        endpoint = f'/catalogs/{catalog_id}/albums/{album_id}/assets?limit=1'
        page = self._make_request(access_token, 'GET', endpoint)

        asset_id = None
        if page and page.get('resources'):
            first_asset = page['resources'][0]
            # Extract asset ID from the resource structure
            asset_id = first_asset.get('asset', {}).get('id')

        # Cache empty albums too (asset_id None) so they don't re-query on every page view
        with self._first_asset_lock:
            self._first_asset_cache[key] = (time.monotonic() + self.FIRST_ASSET_TTL, asset_id)
            self._first_asset_cache.move_to_end(key)
            while len(self._first_asset_cache) > self.FIRST_ASSET_CACHE_SIZE:
                self._first_asset_cache.popitem(last=False)

        return asset_id

    def get_asset_rendition(self, access_token, asset_id, rendition_type='2048'):
        """
        Get rendition image data for an asset

        Args:
            access_token: OAuth access token
            asset_id: Asset ID
            rendition_type: Type of rendition (e.g., '2048', 'thumbnail2x', 'thumbnail')

        Returns:
            bytes: Image binary data
        """
        _check_id(asset_id, 'asset id')
        _check_id(rendition_type, 'rendition type')
        catalog_id = self._get_catalog_id(access_token)

        url = f"{self.API_BASE_URL}/catalogs/{catalog_id}/assets/{asset_id}/renditions/{rendition_type}"
        headers = self._get_headers(access_token)

        # Log request
        safe_headers = headers.copy()
        if 'Authorization' in safe_headers:
            safe_headers['Authorization'] = 'Bearer ***REDACTED***'
        logger.info(f"Lightroom API Request: GET {url}")
        logger.debug(f"Request headers: {safe_headers}")

        with self._api_slots:
            response = requests.get(url, headers=headers, verify=False, timeout=self.REQUEST_TIMEOUT)

        # Log response
        logger.info(f"Lightroom API Response: {response.status_code} from GET {url}")
        logger.debug(f"Response Content-Type: {response.headers.get('Content-Type')}")
        logger.debug(f"Response body length: {len(response.content)} bytes")

        # Handle token expiration
        if response.status_code == 401:
            logger.error(f"Access token rejected for request to {url}")
            raise TokenExpiredError("Access token expired. Please re-authenticate.")

        self._raise_if_rate_limited(response)
        response.raise_for_status()

        return response.content
