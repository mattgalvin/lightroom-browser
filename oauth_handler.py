"""
OAuth2 Handler for Adobe Lightroom API
Handles authentication flow using OAuth 2.0 authorization code grant
"""

import logging
import requests
from urllib.parse import urlencode
from requests.auth import HTTPBasicAuth

# Set up logger
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30


class OAuthHandler:
    """Handles OAuth2 authentication with Adobe Lightroom API.

    This class holds no per-user state. The CSRF `state` value is generated
    and validated by the caller (stored in the user's session).
    """

    # Adobe OAuth endpoints
    AUTHORIZATION_URL = "https://ims-na1.adobelogin.com/ims/authorize/v2"
    TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"

    # Adobe OAuth scopes
    # Requested scopes:
    # - offline_access: allow refresh tokens for long-lived access
    # - AdobeID: access basic Adobe account information
    # - lr_partner_rendition_apis: access rendition-related Lightroom partner APIs
    # - openid: standard OpenID Connect scope
    # - lr_partner_apis: access general Lightroom partner APIs
    SCOPE = "offline_access,AdobeID,lr_partner_rendition_apis,openid,lr_partner_apis"

    def __init__(self, client_id, client_secret, redirect_uri):
        """
        Initialize OAuth handler

        Args:
            client_id: Adobe Client ID
            client_secret: Adobe Client Secret
            redirect_uri: OAuth redirect URI (must match Adobe Console configuration)
        """
        if not client_id or not client_secret:
            raise ValueError("Client ID and Client Secret are required")

        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def get_authorization_url(self, state):
        """
        Generate authorization URL for OAuth2 flow

        Args:
            state: Random CSRF token. The caller must store it in the user's
                   session and compare it with the value returned on callback.

        Returns:
            str: Authorization URL to redirect user to
        """
        if not state:
            raise ValueError("A state value is required")

        params = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': self.SCOPE,
            'state': state
        }

        return f"{self.AUTHORIZATION_URL}?{urlencode(params)}"

    def _token_headers(self):
        return {
            'Content-Type': 'application/x-www-form-urlencoded',
            # Include Adobe API key header on token requests
            'x-api-key': self.client_id,
        }

    def get_access_token(self, authorization_code):
        """
        Exchange authorization code for access token

        Args:
            authorization_code: Authorization code from callback

        Returns:
            dict: Token response containing access_token, refresh_token, etc.

        Raises:
            requests.HTTPError: If token exchange fails
        """
        data = {
            'grant_type': 'authorization_code',
            'code': authorization_code,
            'redirect_uri': self.redirect_uri
        }

        headers = self._token_headers()

        # Use HTTP Basic Authentication for client credentials
        auth = HTTPBasicAuth(self.client_id, self.client_secret)

        # Log request (mask sensitive data)
        safe_data = data.copy()
        safe_data['code'] = '***REDACTED***'
        logger.info(f"OAuth Token Request: POST {self.TOKEN_URL}")
        logger.debug(f"Request data: {safe_data}")

        response = requests.post(
            self.TOKEN_URL, data=data, headers=headers, auth=auth, timeout=REQUEST_TIMEOUT
        )

        logger.info(f"OAuth Token Response: {response.status_code} from POST {self.TOKEN_URL}")

        if not response.ok:
            error_detail = "Unknown error"
            try:
                error_json = response.json()
                error_detail = error_json.get('error_description', error_json.get('error', str(response.text)))
            except ValueError:
                error_detail = response.text or f"HTTP {response.status_code}"

            logger.error(f"Token exchange failed: {error_detail} (Status: {response.status_code})")
            raise requests.HTTPError(
                f"Token exchange failed: {error_detail} (Status: {response.status_code})"
            )

        logger.info("Access token obtained successfully")
        return response.json()

    def refresh_access_token(self, refresh_token):
        """
        Refresh an expired access token

        Args:
            refresh_token: Refresh token from initial authentication

        Returns:
            dict: New token response
        """
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token
        }

        headers = self._token_headers()

        # Use HTTP Basic Authentication for client credentials
        auth = HTTPBasicAuth(self.client_id, self.client_secret)

        logger.info(f"OAuth Token Refresh Request: POST {self.TOKEN_URL}")

        response = requests.post(
            self.TOKEN_URL, data=data, headers=headers, auth=auth, timeout=REQUEST_TIMEOUT
        )

        logger.info(f"OAuth Token Refresh Response: {response.status_code} from POST {self.TOKEN_URL}")

        try:
            response.raise_for_status()
            logger.info("Access token refreshed successfully")
        except requests.HTTPError:
            logger.error(f"Token refresh failed: {response.status_code} - {response.text}")
            raise

        return response.json()
