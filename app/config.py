import os
from dotenv import load_dotenv

load_dotenv()

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")
SESSION_SECRET = os.getenv("SESSION_SECRET", "ultrasecretsessionsecret2025")

if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
    raise RuntimeError("TENANT_ID, CLIENT_ID, CLIENT_SECRET must be set in .env")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"

# Redirect URI must match one configured in Azure app registration
REDIRECT_PATH = "/auth/callback"
REDIRECT_URI = f"{APP_BASE_URL}{REDIRECT_PATH}"

# Minimal scopes to sign in and read basic profile
SCOPES = ["User.Read"]

S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")