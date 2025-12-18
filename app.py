"""
Lightroom Photo Gallery Application
Main Flask application for displaying photos from Adobe Lightroom albums
"""

import os
import logging
from flask import Flask, render_template, redirect, url_for, session, request, Response
from dotenv import load_dotenv
from lightroom_client import LightroomClient
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

# Load pagination settings from environment
ALBUMS_PER_PAGE = int(os.getenv('ALBUMS_PER_PAGE', '8'))
PHOTOS_PER_PAGE = int(os.getenv('PHOTOS_PER_PAGE', '20'))

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')

# Initialize OAuth handler
oauth_handler = OAuthHandler(
    client_id=os.getenv('ADOBE_CLIENT_ID'),
    client_secret=os.getenv('ADOBE_CLIENT_SECRET'),
    redirect_uri=os.getenv('ADOBE_REDIRECT_URI', 'https://localhost:8443/callback')
)

# Initialize Lightroom client
lightroom_client = LightroomClient(oauth_handler)


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

    auth_url = oauth_handler.get_authorization_url()
    return redirect(auth_url)


@app.route('/callback')
def callback():
    """OAuth2 callback handler"""
    code = request.args.get('code')
    error = request.args.get('error')
    state = request.args.get('state')
    
    if error:
        error_description = request.args.get('error_description', error)
        return f"Authentication error: {error_description}", 400
    
    if not code:
        return "No authorization code provided", 400
    
    try:
        # Exchange authorization code for access token
        # Pass state parameter for validation
        token_data = oauth_handler.get_access_token(code, state=state)
        session['access_token'] = token_data['access_token']
        session['refresh_token'] = token_data.get('refresh_token')
        session['expires_in'] = token_data.get('expires_in', 3600)
        
        return redirect(url_for('albums'))
    except Exception as e:
        # Provide detailed error message
        error_msg = str(e)
        return f"Error during authentication: {error_msg}", 500


@app.route('/albums')
def albums():
    """Display list of albums with infinite scroll"""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    try:
        access_token = session['access_token']
        albums_list, next_name_after = lightroom_client.get_albums_page(
            access_token, limit=ALBUMS_PER_PAGE, name_after=None
        )

        # Enrich each album with the first asset ID for thumbnail display
        for album in albums_list:
            album_id = album.get('id')
            if album_id:
                first_asset_id = lightroom_client.get_album_first_asset(access_token, album_id)
                album['first_asset_id'] = first_asset_id

        return render_template(
            'albums.html',
            albums=albums_list,
            next_name_after=next_name_after,
        )
    except Exception as e:
        return f"Error fetching albums: {str(e)}", 500


@app.route('/api/albums')
def albums_api():
    """API endpoint to fetch albums as JSON for infinite scroll"""
    if 'access_token' not in session:
        return {"error": "Unauthorized"}, 401

    name_after = request.args.get('name_after')
    if not name_after:
        return {"error": "name_after parameter required"}, 400

    try:
        access_token = session['access_token']
        albums_list, next_name_after = lightroom_client.get_albums_page(
            access_token, limit=ALBUMS_PER_PAGE, name_after=name_after
        )

        # Enrich and transform albums to simpler format
        albums_data = []
        for album in albums_list:
            album_id = album.get('id')
            if album_id:
                first_asset_id = lightroom_client.get_album_first_asset(access_token, album_id)
                payload = album.get('payload', {})
                albums_data.append({
                    'id': album_id,
                    'name': payload.get('name', 'Untitled Album'),
                    'asset_count': payload.get('assetCount', 0),
                    'first_asset_id': first_asset_id
                })

        return {
            'albums': albums_data,
            'next_name_after': next_name_after
        }
    except Exception as e:
        return {"error": str(e)}, 500


@app.route('/album/<album_id>')
def album_view(album_id):
    """Display photos from a specific album"""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    page_url = request.args.get('page_url')
    if not page_url:
        page_url = None

    try:
        access_token = session['access_token']
        album_info = lightroom_client.get_album(access_token, album_id)
        photos, next_url, prev_url = lightroom_client.get_album_assets_page(
            access_token, album_id, limit=PHOTOS_PER_PAGE, page_url=page_url
        )

        return render_template(
            'gallery.html',
            album=album_info,
            photos=photos,
            next_url=next_url,
            prev_url=prev_url,
            album_id=album_id,
        )
    except Exception as e:
        return f"Error fetching album photos: {str(e)}", 500


@app.route('/api/album/<album_id>/photos')
def album_photos_api(album_id):
    """API endpoint to fetch album photos as JSON for infinite scroll"""
    if 'access_token' not in session:
        return {"error": "Unauthorized"}, 401

    page_url = request.args.get('page_url')
    if not page_url:
        return {"error": "page_url parameter required"}, 400

    try:
        access_token = session['access_token']
        photos, next_url, prev_url = lightroom_client.get_album_assets_page(
            access_token, album_id, limit=PHOTOS_PER_PAGE, page_url=page_url
        )

        # Transform photos to simpler format
        photos_data = []
        for photo in photos:
            asset = photo.get('asset', {})
            asset_id = asset.get('id')
            if asset_id:
                photos_data.append({
                    'asset_id': asset_id,
                    'filename': asset.get('payload', {}).get('importSource', {}).get('fileName', 'Photo')
                })

        return {
            'photos': photos_data,
            'next_url': next_url
        }
    except Exception as e:
        return {"error": str(e)}, 500


@app.route('/thumbnail/<asset_id>')
def thumbnail(asset_id):
    """Proxy endpoint to fetch and serve asset renditions"""
    if 'access_token' not in session:
        return "Unauthorized", 401

    # Get rendition type from query parameter, default to thumbnail2x
    rendition_type = request.args.get('type', 'thumbnail2x')

    # Validate rendition type to prevent arbitrary values
    allowed_types = ['thumbnail', 'thumbnail2x', '640', '1280', '1920', '2048', '2560']
    if rendition_type not in allowed_types:
        return f"Invalid rendition type. Allowed: {', '.join(allowed_types)}", 400

    try:
        access_token = session['access_token']
        # Fetch image data from Lightroom API
        image_data = lightroom_client.get_asset_rendition(
            access_token, asset_id, rendition_type=rendition_type
        )

        # Return image with appropriate content type
        return Response(image_data, mimetype='image/jpeg')
    except Exception as e:
        # Return a simple error response
        return f"Error fetching image: {str(e)}", 500


@app.route('/logout')
def logout():
    """Logout and clear session"""
    session.clear()
    return redirect(url_for('login'))


if __name__ == '__main__':
    import ssl
    
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

