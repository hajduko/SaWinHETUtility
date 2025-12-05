import os
import io
import json as pyjson  # avoid name clash with form field "json"

from .auth import acquire_token_by_authorization_code, get_auth_url
import boto3
import msal

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from itsdangerous import URLSafeSerializer

from .config import SESSION_SECRET, S3_BUCKET_NAME, CLIENT_ID, AUTHORITY, CLIENT_SECRET, REDIRECT_PATH
from .core import transform_json

def get_current_user(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

def require_admin(user=Depends(get_current_user)):
    roles = user.get("roles") or []
    if "Admin" not in roles:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return user

def get_app_only_token() -> str:
    """
    Get an app-only Microsoft Graph access token.
    Uses application permissions (Sites.ReadWrite.All).
    """
    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Failed to get app-only token: {result}")
    return result["access_token"]

s3_client = boto3.client("s3")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

templates = Jinja2Templates(directory="templates")
state_serializer = URLSafeSerializer(SESSION_SECRET, salt="state-salt")

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """
    If user is logged in, show home page.
    Otherwise show login page.
    """
    user = request.session.get("user")
    if user:
        return RedirectResponse(url='/dashboard')
    return templates.TemplateResponse("login.html", {"request": request, "user": None})

@app.get("/login")
async def login(request: Request):
    """
    Redirect to Microsoft login.
    """
    # Create a CSRF-safe state and store it in session
    state = state_serializer.dumps({"csrf": "token"})
    request.session["state"] = state

    auth_url = get_auth_url(state)
    return RedirectResponse(url=auth_url)

@app.get("/logout")
async def logout(request: Request):
    """
    Clear local session and redirect to Microsoft's logout to clear SSO.
    """
    user = request.session.get("user")
    request.session.clear()

    # You can also redirect to https://login.microsoftonline.com/common/oauth2/v2.0/logout
    # with a post_logout_redirect_uri to your app.
    # Here we just redirect back to local home.
    return RedirectResponse(url="/")

@app.get(REDIRECT_PATH)
async def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """
    Microsoft redirects back here after login.
    """
    if error:
        # For example: access_denied if user cancels
        return templates.TemplateResponse("error.html", {"request": request, "message": f"Login failed: {error}", "user": request.session.get("user")})

    if not code or not state:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Missing code or state", "user": request.session.get("user")})

    # Validate state (basic CSRF protection)
    saved_state = request.session.get("state")
    if not saved_state or saved_state != state:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Invalid state", "user": request.session.get("user")})

    # Exchange authorization code for tokens
    result = acquire_token_by_authorization_code(code)

    if "error" in result:
        msg = result.get("error_description") or result["error"]
        return templates.TemplateResponse("error.html", {"request": request, "message": f"Token error: {msg}", "user": request.session.get("user")})

    id_token_claims = result.get("id_token_claims", {})

    roles = id_token_claims.get("roles", []) or []
    # roles will be a list like ["Admin", "User"] depending on assignment

    request.session["user"] = {
        "name": id_token_claims.get("name"),
        "oid": id_token_claims.get("oid"),
        "email": id_token_claims.get("preferred_username") or id_token_claims.get("email"),
        "tid": id_token_claims.get("tid"),
        "roles": roles,
        "is_admin": "Admin" in roles,
    }

    # Clear one-time state
    request.session.pop("state", None)

    return RedirectResponse(url="/")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user=Depends(get_current_user)):
    # List JSON files from S3
    try:
        response = s3_client.list_objects_v2(Bucket=S3_BUCKET_NAME)
    except Exception as e:
        # If listing fails, show empty list but render page
        print(f"Error listing S3 objects: {e}")
        json_files = []
    else:
        contents = response.get("Contents", []) or []
        json_files = []
        for obj in contents:
            key = obj["Key"]
            if key.lower().endswith(".json"):
                json_files.append(
                    {
                        "key": key,
                        "name": os.path.basename(key),
                    }
                )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "json_files": json_files,
        },
    )

REQUIRED_CATEGORIES = {
    "coverPhoto",
    "characteristicHeatExchanger",
    "characteristicOpeningStructure",
}

@app.post("/convert")
async def convert(
    request: Request,
    json_key: str = Form(...),
    pdf: UploadFile = File(...),
    images: list[UploadFile] = File(...),
    categories: list[str] = Form(...),
    notes: list[str] = Form(...),
):
    # Basic consistency checks
    if len(images) != len(categories) and len(images) != len(notes):
        raise HTTPException(status_code=400, detail="Number of images and categories must match.")

    # Enforce required categories at least once
    present_cats = set(categories)
    if not REQUIRED_CATEGORIES.issubset(present_cats):
        missing = REQUIRED_CATEGORIES - present_cats
        raise HTTPException(
            status_code=400,
            detail=f"Missing required image categories: {', '.join(sorted(missing))}",
        )

    # --- Download JSON from S3 ---
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=json_key)
        raw_json_bytes = obj["Body"].read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to download JSON from S3: {e}")

    # Parse JSON
    try:
        input_obj = pyjson.loads(raw_json_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON content in S3 object.")

    # Read PDF bytes
    pdf_bytes = await pdf.read()

    # Read image bytes and assemble metadata (note = filename without extension)
    images_meta = []
    for img_file, category, note in zip(images, categories, notes):
        content = await img_file.read()

        images_meta.append(
            {
                "content": content,
                "note": note,
                "category": category,
            }
        )

    # Transform using your core logic
    result_dict = transform_json(input_obj, pdf_bytes, images_meta)

    # Encode to JSON bytes
    output_bytes = pyjson.dumps(result_dict, ensure_ascii=False, indent=2).encode("utf-8")

    # --- Delete original JSON from S3 after successful conversion ---
    try:
        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=json_key)
    except Exception as e:
        # Not fatal for the download; just log it
        print(f"Failed to delete original JSON from S3 ({json_key}): {e}")

    lead_code = json_key.split("-")[0]

    # Return as downloadable file
    return StreamingResponse(
        io.BytesIO(output_bytes),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{lead_code}.json"'},
    )