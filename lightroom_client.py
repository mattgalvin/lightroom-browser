"""
Lightroom API Client
Handles API calls to Adobe Lightroom API
"""

import re
import json
import logging
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from oauth_handler import OAuthHandler

# Set up logger
logger = logging.getLogger(__name__)

class LightroomClient:
    """Client for interacting with Adobe Lightroom API"""
    
    catalog = None
    
    # Lightroom API base URL
    API_BASE_URL = "https://lr.adobe.io/v2"
    
    def __init__(self, oauth_handler):
        """
        Initialize Lightroom client
        
        Args:
            oauth_handler: OAuthHandler instance for authentication
        """
        self.oauth_handler = oauth_handler
    
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
            url = endpoint
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

        response = requests.request(method, url, headers=headers, **kwargs)

        # Log response details
        logger.info(f"Lightroom API Response: {response.status_code} from {method} {url}")
        logger.debug(f"Response headers: {dict(response.headers)}")
        logger.debug(f"Response body length: {len(response.text)} characters")

        # Handle token expiration
        if response.status_code == 401:
            logger.error(f"Access token expired for request to {url}")
            raise Exception("Access token expired. Please re-authenticate.")

        response.raise_for_status()

        # Process JSON response to strip while(1){} prefix
        # Adobe Lightroom API prepends this to all JSON responses
        resp = self._process_json_response(response.text)
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
        Get user's catalog information
        
        Args:
            access_token: OAuth access token
        
        Returns:
            dict: Catalog information
        """
        if (self.catalog is not None):
            return self.catalog
        else:
            self.catalog = self._make_request(access_token, 'GET', '/catalog')
            return self.catalog
    

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
        catalog = self.get_catalog(access_token)
        if not catalog:
            raise Exception("Could not retrieve catalog")
        catalog_id = catalog.get('id')
        if not catalog_id:
            raise Exception("Could not retrieve catalog ID")

        # Build endpoint with name_after parameter if provided
        endpoint = f'/catalogs/{catalog_id}/albums?limit={int(limit)}'
        if name_after:
            endpoint += f'&name_after={name_after}'

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
            # Parse the next link to extract name_after parameter
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
        catalog = self.get_catalog(access_token)
        if not catalog:
            raise Exception("Could not retrieve catalog")
        catalog_id = catalog.get('id')
        if not catalog_id:
            raise Exception("Could not retrieve catalog ID")
        
        return self._make_request(access_token, 'GET', f'/catalogs/{catalog_id}/albums/{album_id}')
    

    def get_album_assets_page(self, access_token, album_id, limit=20, page_url=None):
        """
        Get a single page of assets for an album with limit and pagination links.
        """
        catalog = self.get_catalog(access_token)
        if not catalog:
            raise Exception("Could not retrieve catalog")
        catalog_id = catalog.get('id')
        if not catalog_id:
            raise Exception("Could not retrieve catalog ID")

        if page_url:
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
        try:
            catalog = self.get_catalog(access_token)
            if not catalog:
                raise Exception("Could not retrieve catalog")
            catalog_id = catalog.get('id')
            if not catalog_id:
                raise Exception("Could not retrieve catalog ID")

            # Fetch just the first asset (limit=1)
            endpoint = f'/catalogs/{catalog_id}/albums/{album_id}/assets?limit=1'
            page = self._make_request(access_token, 'GET', endpoint)

            if page and page.get('resources'):
                first_asset = page['resources'][0]
                # Extract asset ID from the resource structure
                return first_asset.get('asset', {}).get('id')

            return None
        except Exception as e:
            logger.warning(f"Failed to fetch first asset for album {album_id}: {str(e)}")
            return None

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
        catalog = self.get_catalog(access_token)
        if not catalog:
            raise Exception("Could not retrieve catalog")
        catalog_id = catalog.get('id')
        if not catalog_id:
            raise Exception("Could not retrieve catalog ID")

        url = f"{self.API_BASE_URL}/catalogs/{catalog_id}/assets/{asset_id}/renditions/{rendition_type}"
        headers = self._get_headers(access_token)

        # Log request
        safe_headers = headers.copy()
        if 'Authorization' in safe_headers:
            safe_headers['Authorization'] = 'Bearer ***REDACTED***'
        logger.info(f"Lightroom API Request: GET {url}")
        logger.debug(f"Request headers: {safe_headers}")

        response = requests.get(url, headers=headers)

        # Log response
        logger.info(f"Lightroom API Response: {response.status_code} from GET {url}")
        logger.debug(f"Response Content-Type: {response.headers.get('Content-Type')}")
        logger.debug(f"Response body length: {len(response.content)} bytes")

        # Handle token expiration
        if response.status_code == 401:
            logger.error(f"Access token expired for request to {url}")
            raise Exception("Access token expired. Please re-authenticate.")

        response.raise_for_status()

        return response.content

