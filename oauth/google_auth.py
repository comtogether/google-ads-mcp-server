"""
Google Ads OAuth Authentication - supports both local and FastMCP deployment
"""

import os
import json
import requests
import logging
from typing import Dict, Any

# Google Auth libraries
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Constants
SCOPES = ['https://www.googleapis.com/auth/adwords']
API_VERSION = "v23"

# Environment variables
GOOGLE_ADS_CLIENT_ID = os.environ.get("GOOGLE_ADS_CLIENT_ID")
GOOGLE_ADS_CLIENT_SECRET = os.environ.get("GOOGLE_ADS_CLIENT_SECRET")
GOOGLE_ADS_DEVELOPER_TOKEN = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
GOOGLE_ADS_REFRESH_TOKEN = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN")  # NEW: For FastMCP

def format_customer_id(customer_id: str) -> str:
    """Format customer ID to ensure it's 10 digits without dashes."""
    customer_id = str(customer_id)
    customer_id = customer_id.replace('\"', '').replace('"', '')
    customer_id = ''.join(char for char in customer_id if char.isdigit())
    return customer_id.zfill(10)

def get_oauth_credentials():
    """Get and refresh OAuth user credentials - supports both local and cloud."""
    if not GOOGLE_ADS_CLIENT_ID or not GOOGLE_ADS_CLIENT_SECRET:
        raise ValueError(
            "GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET environment variables not set. "
            "Please set them to your OAuth credentials."
        )
    
    creds = None
    
    # NEW: Check if refresh token is provided (for FastMCP/cloud deployment)
    if GOOGLE_ADS_REFRESH_TOKEN:
        logger.info("Using refresh token from environment variable")
        creds = Credentials(
            token=None,
            refresh_token=GOOGLE_ADS_REFRESH_TOKEN,
            client_id=GOOGLE_ADS_CLIENT_ID,
            client_secret=GOOGLE_ADS_CLIENT_SECRET,
            token_uri='https://oauth2.googleapis.com/token',
            scopes=SCOPES
        )
        
        # Refresh to get access token
        try:
            logger.info("Refreshing token to get access token")
            creds.refresh(Request())
            logger.info("Token successfully refreshed")
            return creds
        except RefreshError as e:
            logger.error(f"Refresh token is invalid: {e}")
            raise ValueError(
                "GOOGLE_ADS_REFRESH_TOKEN is invalid or expired. "
                "Please generate a new refresh token by running authentication locally."
            )
        except Exception as e:
            logger.error(f"Unexpected error refreshing token: {e}")
            raise
    
    # Local development: Use token file or interactive flow
    token_path = os.path.join(os.path.expanduser('~'), '.google_ads_token.json')
    
    # Load existing token if it exists
    if os.path.exists(token_path):
        try:
            logger.info(f"Loading existing OAuth token from {token_path}")
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            logger.warning(f"Error loading existing token: {e}")
            creds = None
    
    # Check if credentials are valid
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("Refreshing expired OAuth token")
                creds.refresh(Request())
                logger.info("Token successfully refreshed")
            except RefreshError as e:
                logger.warning(f"Token refresh failed: {e}, will get new token")
                creds = None
            except Exception as e:
                logger.error(f"Unexpected error refreshing token: {e}")
                raise
        
        # Need new credentials - run OAuth flow (LOCAL ONLY)
        if not creds:
            logger.info("Starting OAuth authentication flow (local only)")
            
            # Check if we're in a cloud environment
            is_cloud = os.environ.get("FASTMCP_ENV") or os.environ.get("RAILWAY_ENVIRONMENT")
            if is_cloud:
                raise ValueError(
                    "Interactive OAuth flow cannot run in cloud environment. "
                    "Please set GOOGLE_ADS_REFRESH_TOKEN environment variable. "
                    "Run authentication locally first to generate the refresh token."
                )
            
            try:
                # Build client configuration from environment variables
                client_config = {
                    "installed": {
                        "client_id": GOOGLE_ADS_CLIENT_ID,
                        "client_secret": GOOGLE_ADS_CLIENT_SECRET,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                        "redirect_uris": ["http://localhost", "urn:ietf:wg:oauth:2.0:oob"]
                    }
                }
                
                # Create flow
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                
                # Run OAuth flow with automatic local server
                try:
                    creds = flow.run_local_server(port=0)
                    logger.info("OAuth flow completed successfully using local server")
                except Exception as e:
                    logger.error(f"Local server failed: {e}")
                    logger.error("Cannot use console flow in cloud environment")
                    raise ValueError(
                        "OAuth flow failed. For cloud deployment, please set "
                        "GOOGLE_ADS_REFRESH_TOKEN environment variable."
                    )
                
                # Print refresh token for user to save
                if creds and creds.refresh_token:
                    print("\n" + "="*60)
                    print("IMPORTANT: Save this refresh token for FastMCP deployment:")
                    print("="*60)
                    print(f"GOOGLE_ADS_REFRESH_TOKEN={creds.refresh_token}")
                    print("="*60 + "\n")
                
            except Exception as e:
                logger.error(f"OAuth flow failed: {e}")
                raise
        
        # Save the credentials for local use
        if creds:
            try:
                logger.info(f"Saving credentials to {token_path}")
                os.makedirs(os.path.dirname(token_path), exist_ok=True)
                with open(token_path, 'w') as f:
                    f.write(creds.to_json())
                logger.info("Credentials saved successfully")
            except Exception as e:
                logger.warning(f"Could not save credentials: {e}")
    
    return creds

def get_headers_with_auto_token() -> Dict[str, str]:
    """Get API headers with automatically managed token - integrated OAuth."""
    if not GOOGLE_ADS_DEVELOPER_TOKEN:
        raise ValueError("GOOGLE_ADS_DEVELOPER_TOKEN environment variable not set")
    
    # This will automatically trigger OAuth flow if needed
    creds = get_oauth_credentials()
    
    headers = {
        'Authorization': f'Bearer {creds.token}',
        'Developer-Token': GOOGLE_ADS_DEVELOPER_TOKEN.strip('"').strip("'"),
        'Content-Type': 'application/json'
    }
    
    return headers

def execute_gaql(customer_id: str, query: str, manager_id: str = "") -> Dict[str, Any]:
    """Execute GAQL using the non-streaming search endpoint."""
    # This will automatically trigger OAuth if needed
    headers = get_headers_with_auto_token()
    
    formatted_customer_id = format_customer_id(customer_id)
    url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
    
    if manager_id:
        headers['login-customer-id'] = format_customer_id(manager_id)

    payload = {'query': query}
    resp = requests.post(url, headers=headers, json=payload)
    
    if not resp.ok:
        raise Exception(f"Error executing GAQL: {resp.status_code} {resp.reason} - {resp.text}")
    
    data = resp.json()
    results = data.get('results', [])
    return {
        'results': results,
        'query': query,
        'totalRows': len(results),
    }


# How to Use This Updated Code

# Step 1: Generate Refresh Token Locally

# ============================================================
# IMPORTANT: Save this refresh token for FastMCP deployment:
# ============================================================
# GOOGLE_ADS_REFRESH_TOKEN=1//0gABC...xyz123
# ============================================================

# Step 2: Deploy to FastMCP

# Add these environment variables in FastMCP:
# GOOGLE_ADS_CLIENT_ID=your-client-id
# GOOGLE_ADS_CLIENT_SECRET=your-client-secret
# GOOGLE_ADS_DEVELOPER_TOKEN=your-developer-token
# GOOGLE_ADS_REFRESH_TOKEN=1//0gABC...xyz123