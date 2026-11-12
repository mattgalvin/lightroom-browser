# Lightroom Photo Gallery

A Python Flask web application that displays photos from Adobe Lightroom albums using the Lightroom API with OAuth2 authentication.

## Features

- OAuth2 authentication with Adobe Lightroom
- Browse your Lightroom albums
- View photos from selected albums
- Modern, responsive web interface

## Prerequisites

- Python 3.7 or higher
- Adobe Developer Account
- Lightroom account with photos

## Setup

### 1. Create and Activate Virtual Environment

**On macOS/Linux:**
```bash
cd lightroom_gallery
chmod +x setup.sh
./setup.sh
```

**On Windows:**
```cmd
cd lightroom_gallery
setup.bat
```

**Manual Setup (Alternative):**
```bash
# Create virtual environment
python3 -m venv venv

# Activate virtual environment
# On macOS/Linux:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Adobe Developer Console Setup

1. Go to [Adobe Developer Console](https://developer.adobe.com/console)
2. Create a new project or select an existing one
3. Add the Lightroom API to your project
4. Create an **OAuth Web App** credential:
   - Set the redirect URI to: `https://localhost:8443/callback` (HTTPS)
   - Note your Client ID and Client Secret

### 3. Configure Environment Variables

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and add your Adobe credentials:
   ```
   ADOBE_CLIENT_ID=your_actual_client_id
   ADOBE_CLIENT_SECRET=your_actual_client_secret
   ADOBE_REDIRECT_URI=https://localhost:8443/callback
   FLASK_SECRET_KEY=generate-a-random-secret-key-here
   ```

## Running the Application

**Important:** Make sure your virtual environment is activated before running the application.

```bash
# Activate virtual environment (if not already activated)
# On macOS/Linux:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Run the application
python app.py
```

The application will start on `https://localhost:8443` (HTTPS). By default it listens on
`127.0.0.1` only; set `HOST=0.0.0.0` in `.env` to expose it on your network (this is refused
when `FLASK_DEBUG=True`, because the Werkzeug debugger must never be reachable from the network).

## Running the Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

The tests use mocks only: they never contact Adobe and never read your real credentials.

## Usage

1. Navigate to `https://localhost:8443` in your browser
2. Accept the security warning for the self-signed certificate (development only)
3. Click "Sign in with Adobe" to authenticate
4. After authentication, you'll see a list of your Lightroom albums
5. Click on an album to view its photos

## Project Structure

```
lightroom_gallery/
├── app.py                 # Main Flask application
├── oauth_handler.py       # OAuth2 authentication handler
├── lightroom_client.py    # Lightroom API client
├── token_store.py         # Server-side storage for Adobe tokens
├── tests/                 # pytest suite (no network access needed)
├── requirements.txt       # Python dependencies
├── requirements-dev.txt   # Test dependencies
├── setup.sh              # Setup script (macOS/Linux)
├── setup.bat             # Setup script (Windows)
├── venv/                 # Virtual environment (created by setup)
├── .env.example          # Environment variables template
├── .env                  # Your environment variables (create this)
├── templates/            # HTML templates
│   ├── base.html
│   ├── albums.html
│   └── gallery.html
└── static/              # Static files (CSS, JS, images)
    └── css/
        └── style.css
```

## API Documentation

- [Lightroom API Documentation](https://developer.adobe.com/lightroom/lightroom-api-docs)
- [Adobe Authentication Guide](https://developer.adobe.com/developer-console/docs/guides/authentication/)

## Security Notes

- Never commit your `.env` file to version control
- Use a strong `FLASK_SECRET_KEY` in production
- The redirect URI in `.env` must exactly match the one configured in Adobe Console
- For production deployments, use HTTPS and update the redirect URI accordingly
- Adobe access/refresh tokens are kept server-side (`token_store.py`); the browser cookie only holds a random session id. The store is in memory and per process, so restarting the app logs everyone out, and running multiple worker processes would need a shared store (e.g. Redis)

## Troubleshooting

### Authentication Errors

- Verify your Client ID and Client Secret are correct
- Ensure the redirect URI matches exactly (must use `https://` for HTTPS)
- Check that Lightroom API is enabled in your Adobe Developer Console project

### Virtual Environment Issues

- Make sure you've activated the virtual environment before running the application
- If you get "command not found" errors, ensure the virtual environment is activated
- To recreate the virtual environment, delete the `venv` folder and run the setup script again

### No Albums Found

- Make sure you have albums created in Lightroom
- Verify your Lightroom account has photos synced to the cloud

## License

This project is provided as-is for educational purposes.

