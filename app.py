"""
Lightroom Photo Gallery Application
Main Flask application for displaying photos from Adobe Lightroom albums
"""

import os
import time
import secrets
import logging
from functools import wraps

from flask import Flask, render_template, redirect, url_for, session, request, Response, g
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv
from lightroom_client import LightroomClient, TokenExpiredError, InvalidInputError
from oauth_handler import OAuthHandler

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Output to console
        logging.FileHandler('lightroom_gallery.log')  # Output to file
    ]
)

# Set log level from environment variable if provided
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))

logger = logging.getLogger(__name__)

# Load pagination settings from environment
ALBUMS_PER_PAGE = int(os.getenv('ALBUMS_PER_PAGE', '8'))
PHOTOS_PER_PAGE = int(os.getenv('PHOTOS_PER_PAGE', '20'))

# Refresh the access token this many seconds before it actually expires
TOKEN_REFRESH_MARGIN = 60

# Secret key: never run with a well-known value
PLACEHOLDER_SECRET_KEYS = {
    '',
    'dev-secret-key-change-in-production',
    'your-secret-key-change-in-production',
    'generate-a-random-secret-key-here',
}
secret_key = os.getenv('FLASK_SECRET_KEY')
if not secret_key or secret_key in PLACEHOLDER_SECRET_KEYS:
    secret_key = secrets.token_hex(32)
    logger.warning(
        "FLASK_SECRET_KEY is not set (or is a placeholder). Using a random key for this run; "
        "sessions will be invalidated on restart. Set FLASK_SECRET_KEY in .env to avoid this."
    )

app = Flask(__name__)
app.secret_key = secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',  # Lax still allows the top-level redirect back from Adobe
    SESSION_COOKIE_SECURE=True,     # the app is served over HTTPS
)

# Initialize OAuth handler
oauth_handler = OAuthHandler(
    client_id=os.getenv('ADOBE_CLIENT_ID'),
    client_secret=os.getenv('ADOBE_CLIENT_SECRET'),
    redirect_uri=os.getenv('ADOBE_REDIRECT_URI', 'https://localhost:8443/callback')
)

# Initialize Lightroom client
lightroom_client = LightroomClient(oauth_handler)


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def store_tokens(token_data):
    """Save a token response in the session, keeping the old refresh token if none is returned."""
    session['access_token'] = token_data['access_token']
    session['refresh_token'] = token_data.get('refresh_token') or session.get('refresh_token')

    expires_in = float(token_data.get('expires_in', 3600))
    # Adobe IMS reports expires_in in milliseconds; handle seconds too
    if expires_in > 10_000_000:
        expires_in /= 1000.0
    session['expires_at'] = time.time() + expires_in


def get_valid_access_token():
    """
    Return a usable access token for the current session, refreshing it if it is
    (about to be) expired. Returns None if the user must log in again.
    """
    access_token = session.get('access_token')
    if not access_token:
        return None

    if time.time() < session.get('expires_at', 0) - TOKEN_REFRESH_MARGIN:
        return access_token

    refresh_token = session.get('refresh_token')
    if not refresh_token:
        session.clear()
        return None

    try:
        store_tokens(oauth_handler.refresh_access_token(refresh_token))
    except Exception:
        logger.warning("Token refresh failed; asking the user to log in again", exc_info=True)
        session.clear()
        return None

    return session['access_token']


def _error_response(message, status):
    if request.path.startswith('/api/'):
        return {"error": message}, status
    return message, status


def require_auth(view):
    """Ensure a valid access token (refreshing if needed) and expose it as g.access_token."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        token = get_valid_access_token()
        if not token:
            if request.path.startswith(('/api/', '/thumbnail/')):
                return _error_response("Unauthorized", 401)
            return redirect(url_for('login'))
        g.access_token = token
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@app.errorhandler(TokenExpiredError)
def handle_token_expired(e):
    # Adobe rejected the token even though we thought it was valid
    session.clear()
    if request.path.startswith(('/api/', '/thumbnail/')):
        return _error_response("Unauthorized", 401)
    return redirect(url_for('login'))


@app.errorhandler(InvalidInputError)
def handle_invalid_input(e):
    return _error_response("Bad request", 400)


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    if isinstance(e, HTTPException):
        return e
    # Full details go to the log; the browser only gets a generic message
    logger.exception("Unhandled error on %s %s", request.method, request.path)
    return _error_response("Something went wrong. Please try again.", 500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def simplify_photos(photos):
    """Reduce asset resources to the fields the templates and JS need."""
    simplified = []
    for photo in photos:
        asset = photo.get('asset', {})
        asset_id = asset.get('id')
        if asset_id:
            filename = asset.get('payload', {}).get('importSource', {}).get('fileName') or 'Photo'
            simplified.append({'asset_id': asset_id, 'filename': filename})
    return simplified


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    """Home page - redirects to login if not authenticated"""
    if 'access_token' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('albums'))


@app.route('/login')
def login():
    """Display login page"""
    if 'access_token' in session:
        return redirect(url_for('albums'))

    return render_template('login.html')


@app.route('/oauth/start')
def oauth_start():
    """Initiate OAuth2 login flow"""
    if 'access_token' in session:
        return redirect(url_for('albums'))

    # Per-session CSRF state, validated in /callback
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state

    return redirect(oauth_handler.get_authorization_url(state))


@app.route('/callback')
def callback():
    """OAuth2 callback handler"""
    code = request.args.get('code')
    error = request.args.get('error')
    state = request.args.get('state')

    # One-time use: remove the stored state whatever happens next
    expected_state = session.pop('oauth_state', None)

    if error:
        logger.warning("Adobe returned an OAuth error: %s (%s)", error, request.args.get('error_description'))
        return "Authentication failed. Please try signing in again.", 400

    if not expected_state or not state or not secrets.compare_digest(state, expected_state):
        logger.warning("OAuth callback with missing or mismatched state")
        return "Invalid login request. Please try signing in again.", 400

    if not code:
        return "No authorization code provided", 400

    try:
        token_data = oauth_handler.get_access_token(code)
    except Exception:
        logger.exception("Token exchange failed")
        return "Authentication failed. Please try signing in again.", 500

    session.clear()  # start from a clean session after login
    store_tokens(token_data)

    return redirect(url_for('albums'))


@app.route('/albums')
@require_auth
def albums():
    """Display list of albums with infinite scroll"""
    albums_list, next_name_after = lightroom_client.get_albums_page(
        g.access_token, limit=ALBUMS_PER_PAGE, name_after=None
    )

    # Album cover images are loaded lazily by the browser from /api/album/<id>/cover,
    # so no extra Lightroom API calls are made here for albums that aren't on screen.
    return render_template(
        'albums.html',
        albums=albums_list,
        next_name_after=next_name_after,
    )


@app.route('/api/albums')
@require_auth
def albums_api():
    """API endpoint to fetch albums as JSON for infinite scroll"""
    name_after = request.args.get('name_after')
    if not name_after:
        return {"error": "name_after parameter required"}, 400

    albums_list, next_name_after = lightroom_client.get_albums_page(
        g.access_token, limit=ALBUMS_PER_PAGE, name_after=name_after
    )

    albums_data = []
    for album in albums_list:
        album_id = album.get('id')
        if album_id:
            payload = album.get('payload', {})
            albums_data.append({
                'id': album_id,
                'name': payload.get('name', 'Untitled Album'),
                'asset_count': payload.get('assetCount'),
            })

    return {
        'albums': albums_data,
        'next_name_after': next_name_after
    }


@app.route('/api/album/<album_id>/cover')
@require_auth
def album_cover(album_id):
    """Serve an album's cover (its first photo) as a thumbnail"""
    asset_id = lightroom_client.get_album_first_asset(g.access_token, album_id)
    if not asset_id:
        return "No photos", 404

    image_data = lightroom_client.get_asset_rendition(
        g.access_token, asset_id, rendition_type='thumbnail2x'
    )
    response = Response(image_data, mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'private, max-age=300'
    return response


@app.route('/album/<album_id>')
@require_auth
def album_view(album_id):
    """Display photos from a specific album"""
    page_url = request.args.get('page_url') or None

    album_info = lightroom_client.get_album(g.access_token, album_id)
    photos, next_url, prev_url = lightroom_client.get_album_assets_page(
        g.access_token, album_id, limit=PHOTOS_PER_PAGE, page_url=page_url
    )

    return render_template(
        'gallery.html',
        album=album_info,
        photos=simplify_photos(photos),
        next_url=next_url,
        prev_url=prev_url,
        album_id=album_id,
    )


@app.route('/api/album/<album_id>/photos')
@require_auth
def album_photos_api(album_id):
    """API endpoint to fetch album photos as JSON for infinite scroll"""
    page_url = request.args.get('page_url')
    if not page_url:
        return {"error": "page_url parameter required"}, 400

    photos, next_url, _ = lightroom_client.get_album_assets_page(
        g.access_token, album_id, limit=PHOTOS_PER_PAGE, page_url=page_url
    )

    return {
        'photos': simplify_photos(photos),
        'next_url': next_url
    }


@app.route('/thumbnail/<asset_id>')
@require_auth
def thumbnail(asset_id):
    """Proxy endpoint to fetch and serve asset renditions"""
    # Get rendition type from query parameter, default to thumbnail2x
    rendition_type = request.args.get('type', 'thumbnail2x')

    # Validate rendition type to prevent arbitrary values
    allowed_types = ['thumbnail', 'thumbnail2x', '640', '1280', '1920', '2048', '2560']
    if rendition_type not in allowed_types:
        return f"Invalid rendition type. Allowed: {', '.join(allowed_types)}", 400

    image_data = lightroom_client.get_asset_rendition(
        g.access_token, asset_id, rendition_type=rendition_type
    )

    return Response(image_data, mimetype='image/jpeg')


@app.route('/logout')
def logout():
    """Logout and clear session"""
    session.clear()
    return redirect(url_for('login'))


if __name__ == '__main__':
    port = int(os.getenv('PORT', 8443))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

    # SSL configuration
    cert_file = os.getenv('SSL_CERT_FILE', 'cert.pem')
    key_file = os.getenv('SSL_KEY_FILE', 'key.pem')

    ssl_context = None

    # Try to use provided SSL certificates
    if os.path.exists(cert_file) and os.path.exists(key_file):
        ssl_context = (cert_file, key_file)
        print(f"Using SSL certificates: {cert_file}, {key_file}")
    else:
        # Use adhoc SSL context for development (self-signed certificate)
        ssl_context = 'adhoc'
        print("Warning: Using adhoc SSL context (self-signed certificate).")
        print("For production, provide SSL certificates via SSL_CERT_FILE and SSL_KEY_FILE environment variables.")
        print(f"Expected files: {cert_file}, {key_file}")

    app.run(host='0.0.0.0', port=port, debug=debug, ssl_context=ssl_context)
