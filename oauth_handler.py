"""
OAuth2 Handler for Adobe Lightroom API
Handles authentication flow using OAuth 2.0 authorization code grant
"""

import logging
import requests
import secrets
from urllib.parse import urlencode
from requests.auth import HTTPBasicAuth

# Set up logger
logger = logging.getLogger(__name__)


class OAuthHandler:
    """Handles OAuth2 authentication with Adobe Lightroom API"""
    
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
        self.state = None
    
    def get_authorization_url(self):
        """
        Generate authorization URL for OAuth2 flow
        
        Returns:
            str: Authorization URL to redirect user to
        """
        # Generate state for CSRF protection
        self.state = secrets.token_urlsafe(32)
        
        params = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': self.SCOPE,
            'state': self.state
        }
        
        return f"{self.AUTHORIZATION_URL}?{urlencode(params)}"
    
    def get_access_token(self, authorization_code, state=None):
        """
        Exchange authorization code for access token

        Args:
            authorization_code: Authorization code from callback
            state: State parameter for CSRF validation (optional)

        Returns:
            dict: Token response containing access_token, refresh_token, etc.

        Raises:
            ValueError: If state validation fails
            requests.HTTPError: If token exchange fails
        """
        if state and state != self.state:
            raise ValueError("Invalid state parameter")

        data = {
            'grant_type': 'authorization_code',
            'code': authorization_code,
            'redirect_uri': self.redirect_uri
        }

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            # Include Adobe API key header on token request
            'x-api-key': self.client_id,
        }

        # Use HTTP Basic Authentication for client credentials
        auth = HTTPBasicAuth(self.client_id, self.client_secret)

        # Log request (mask sensitive data)
        safe_data = data.copy()
        safe_data['code'] = '***REDACTED***'
        logger.info(f"OAuth Token Request: POST {self.TOKEN_URL}")
        logger.debug(f"Request data: {safe_data}")
        logger.debug(f"Request headers: {headers}")

        try:
            response = requests.post(self.TOKEN_URL, data=data, headers=headers, auth=auth)

            # Log response
            logger.info(f"OAuth Token Response: {response.status_code} from POST {self.TOKEN_URL}")
            logger.debug(f"Response headers: {dict(response.headers)}")

            # If request failed, provide detailed error information
            if not response.ok:
                error_detail = "Unknown error"
                try:
                    error_json = response.json()
                    error_detail = error_json.get('error_description', error_json.get('error', str(response.text)))
                except (ValueError, requests.exceptions.JSONDecodeError):
                    error_detail = response.text or f"HTTP {response.status_code}"

                logger.error(f"Token exchange failed: {error_detail} (Status: {response.status_code})")
                raise requests.HTTPError(
                    f"Token exchange failed: {error_detail} (Status: {response.status_code})"
                )

            # Log success (mask sensitive token data)
            logger.info("Access token obtained successfully")
            return response.json()
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error during token exchange: {str(e)}")
            raise Exception(f"Unexpected error during token exchange: {str(e)}")
    
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

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            # Include Adobe API key header on token refresh request
            'x-api-key': self.client_id,
        }

        # Use HTTP Basic Authentication for client credentials
        auth = HTTPBasicAuth(self.client_id, self.client_secret)

        # Log request (mask sensitive data)
        safe_data = data.copy()
        safe_data['refresh_token'] = '***REDACTED***'
        logger.info(f"OAuth Token Refresh Request: POST {self.TOKEN_URL}")
        logger.debug(f"Request data: {safe_data}")
        logger.debug(f"Request headers: {headers}")

        response = requests.post(self.TOKEN_URL, data=data, headers=headers, auth=auth)

        # Log response
        logger.info(f"OAuth Token Refresh Response: {response.status_code} from POST {self.TOKEN_URL}")
        logger.debug(f"Response headers: {dict(response.headers)}")

        try:
            response.raise_for_status()
            logger.info("Access token refreshed successfully")
        except requests.HTTPError as e:
            logger.error(f"Token refresh failed: {response.status_code} - {response.text}")
            raise

        return response.json()

