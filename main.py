import os
import io
import csv
import json
import qrcode
from datetime import datetime
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from database import db, create_document, get_documents
from schemas import Form as FormSchema, Submission as SubmissionSchema

# Google APIs
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# Firebase Admin for token verification (Auth)
import firebase_admin
from firebase_admin import auth as fb_auth, credentials as fb_credentials

# --- Config ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
MASTER_SPREADSHEET_ID = os.getenv("MASTER_SPREADSHEET_ID")  # Spreadsheet to host tabs per form
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")  # Folder to store uploads
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # JSON string or path
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:3000")

# Initialize Firebase Admin if credentials provided
if not firebase_admin._apps:
    fb_creds_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    try:
        if fb_creds_json:
            cred = fb_credentials.Certificate(json.loads(fb_creds_json))
            firebase_admin.initialize_app(cred)
    except Exception:
        # ignore init error; endpoints that need auth will fail gracefully
        pass

app = FastAPI(title="SmartForm Builder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Helpers ---

def get_google_services():
    """Build Sheets and Drive services from service account credentials."""
    creds = None
    if SERVICE_ACCOUNT_JSON:
        try:
            # Allow passing either full JSON string or a file path
            if SERVICE_ACCOUNT_JSON.strip().startswith("{"):
                info = json.loads(SERVICE_ACCOUNT_JSON)
                creds = Credentials.from_service_account_info(info, scopes=SCOPES)
            else:
                creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=SCOPES)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Invalid Google service account credentials: {e}")
    else:
        raise HTTPException(status_code=500, detail="GOOGLE_SERVICE_ACCOUNT_JSON not set")

    try:
        sheets_service = build("sheets", "v4", credentials=creds)
        drive_service = build("drive", "v3", credentials=creds)
        return sheets_service, drive_service
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize Google services: {e}")


def verify_admin(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """Verify Firebase ID token from Authorization: Bearer <token>. Returns uid."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    token = parts[1]
    try:
        decoded = fb_auth.verify_id_token(token)
        return decoded.get("uid")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


def ensure_master_sheet(sheets_service):
    if not MASTER_SPREADSHEET_ID:
        raise HTTPException(status_code=500, detail="MASTER_SPREADSHEET_ID not set")
    # Optionally validate it exists by a simple get
    try:
        sheets_service.spreadsheets().get(spreadsheetId=MASTER_SPREADSHEET_ID).execute()
    except HttpError as e:
        raise HTTPException(status_code=500, detail=f"Invalid MASTER_SPREADSHEET_ID: {e}")


def create_sheet_tab_for_form(title: str, fields: List[Dict[str, Any]], sheet_name: Optional[str] = None) -> str:
    """Create a new sheet/tab in the master spreadsheet with header row based on fields."""
    sheets_service, _ = get_google_services()
    ensure_master_sheet(sheets_service)

    # Sheet name
    sheet_title = sheet_name or f"{title[:25]}-{int(datetime.utcnow().timestamp())}"

    # Prepare header row: Timestamp + labels + file links
    headers = ["Timestamp"] + [f.get("label", f.get("id")) for f in fields]

    # Add sheet
    requests = [
        {
            "addSheet": {
                "properties": {
                    "title": sheet_title,
                    "gridProperties": {"rowCount": 1000, "columnCount": len(headers) + 2}
                }
            }
        }
    ]

    body = {"requests": requests}
    sheets_service.spreadsheets().batchUpdate(spreadsheetId=MASTER_SPREADSHEET_ID, body=body).execute()

    # Write header row
    sheets_service.spreadsheets().values().update(
        spreadsheetId=MASTER_SPREADSHEET_ID,
        range=f"{sheet_title}!A1",
        valueInputOption="RAW",
        body={"values": [headers]},
    ).execute()

    return sheet_title


def append_submission_to_sheet(sheet_name: str, fields: List[Dict[str, Any]], data: Dict[str, Any]):
    sheets_service, _ = get_google_services()
    ensure_master_sheet(sheets_service)

    headers = ["Timestamp"] + [f.get("label", f.get("id")) for f in fields]
    row = [datetime.utcnow().isoformat()]
    for f in fields:
        fid = f.get("id")
        value = data.get(fid)
        if isinstance(value, list):
            value = ", ".join(map(str, value))
        row.append(value)

    sheets_service.spreadsheets().values().append(
        spreadsheetId=MASTER_SPREADSHEET_ID,
        range=f"{sheet_name}!A1",
        valueInputOption="RAW",
        body={"values": [row]},
    ).execute()


def upload_file_to_drive(file: UploadFile) -> Optional[str]:
    """Upload file to Google Drive, return sharable link."""
    _, drive_service = get_google_services()
    if not DRIVE_FOLDER_ID:
        raise HTTPException(status_code=500, detail="DRIVE_FOLDER_ID not set")

    file_metadata = {
        "name": file.filename,
        "parents": [DRIVE_FOLDER_ID]
    }
    media = MediaIoBaseUpload(file.file, mimetype=file.content_type, resumable=False)
    uploaded = drive_service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink, webContentLink").execute()

    # Make sure file is readable by link
    try:
        drive_service.permissions().create(fileId=uploaded["id"], body={"type": "anyone", "role": "reader"}).execute()
    except Exception:
        pass
    return uploaded.get("webViewLink") or uploaded.get("webContentLink")


# --- Models ---
class CreateFormRequest(BaseModel):
    title: str
    description: Optional[str] = None
    fields: List[Dict[str, Any]]

class CreateFormResponse(BaseModel):
    form_id: str
    share_url: str
    sheet_name: Optional[str]


# --- Routes ---
@app.get("/")
def read_root():
    return {"message": "SmartForm Builder API running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Connected"
            response["database_name"] = db.name if hasattr(db, 'name') else "Unknown"
            response["connection_status"] = "Connected"
            response["collections"] = db.list_collection_names()
    except Exception as e:
        response["database"] = f"Error: {e}"
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


@app.post("/api/forms", response_model=CreateFormResponse)
def create_form(payload: CreateFormRequest, uid: str = Depends(verify_admin)):
    # Create sheet tab and header row
    sheet_name = create_sheet_tab_for_form(payload.title, payload.fields)

    # Create share slug
    slug = f"{payload.title.lower().replace(' ', '-')}-{int(datetime.utcnow().timestamp())}"

    form_doc = FormSchema(
        title=payload.title,
        description=payload.description,
        fields=payload.fields,  # validated at submission
        sheet_name=sheet_name,
        share_slug=slug,
        owner_uid=uid,
    )
    form_id = create_document("form", form_doc)

    share_url = f"{PUBLIC_BASE_URL.rstrip('/')}/f/{slug}"
    return CreateFormResponse(form_id=form_id, share_url=share_url, sheet_name=sheet_name)


@app.get("/api/forms")
def list_forms(uid: str = Depends(verify_admin)):
    forms = get_documents("form")
    # Filter by owner if provided
    result = []
    for f in forms:
        item = {
            "_id": str(f.get("_id")),
            "title": f.get("title"),
            "description": f.get("description"),
            "share_slug": f.get("share_slug"),
            "sheet_name": f.get("sheet_name"),
            "created_at": f.get("created_at"),
        }
        result.append(item)
    return {"forms": result}


@app.get("/api/forms/by-slug/{slug}")
def get_form_by_slug(slug: str):
    doc = db["form"].find_one({"share_slug": slug})
    if not doc:
        raise HTTPException(status_code=404, detail="Form not found")
    doc["_id"] = str(doc["_id"])
    return doc


@app.post("/api/forms/{slug}/submit")
async def submit_form(slug: str, request: Request):
    # Find form
    form_doc = db["form"].find_one({"share_slug": slug})
    if not form_doc:
        raise HTTPException(status_code=404, detail="Form not found")

    content_type = request.headers.get("content-type", "")
    data: Dict[str, Any] = {}
    file_links: Dict[str, str] = {}

    if content_type.startswith("application/json"):
        payload = await request.json()
        data = payload.get("data", {})
    else:
        # Handle multipart form for file uploads
        form = await request.form()
        for k, v in form.multi_items():
            if isinstance(v, UploadFile):
                if v.filename:
                    link = upload_file_to_drive(v)
                    file_links[k] = link
            else:
                # handle checkbox groups (multiple values)
                if k in data:
                    if isinstance(data[k], list):
                        data[k].append(str(v))
                    else:
                        data[k] = [data[k], str(v)]
                else:
                    data[k] = str(v)

    # Basic required validation
    field_map = {f.get("id"): f for f in form_doc.get("fields", [])}
    for fid, field in field_map.items():
        if field.get("required") and not data.get(fid):
            raise HTTPException(status_code=400, detail=f"Missing required field: {field.get('label') or fid}")
        # Email formatting and numeric checks can be extended client-side; keep simple here

    # Store submission
    sub = SubmissionSchema(form_id=str(form_doc.get("_id")), data=data, file_links=file_links)
    sub_id = create_document("submission", sub)

    # Append to Google Sheet
    try:
        combined = {**data, **{k: v for k, v in file_links.items()}}
        append_submission_to_sheet(form_doc.get("sheet_name"), form_doc.get("fields", []), combined)
    except Exception as e:
        # log but don't block success
        print("Sheet append error:", e)

    return {"status": "ok", "submission_id": sub_id}


@app.get("/api/forms/{slug}/analytics")
def form_analytics(slug: str, uid: str = Depends(verify_admin)):
    form_doc = db["form"].find_one({"share_slug": slug})
    if not form_doc:
        raise HTTPException(status_code=404, detail="Form not found")
    count = db["submission"].count_documents({"form_id": str(form_doc["_id"])})
    recent = list(db["submission"].find({"form_id": str(form_doc["_id"])}, sort=[("created_at", -1)]).limit(5))
    for r in recent:
        r["_id"] = str(r["_id"])
    return {"count": count, "recent": recent}


@app.get("/api/forms/{slug}/export/csv")
def export_csv(slug: str, uid: str = Depends(verify_admin)):
    form_doc = db["form"].find_one({"share_slug": slug})
    if not form_doc:
        raise HTTPException(status_code=404, detail="Form not found")
    subs = list(db["submission"].find({"form_id": str(form_doc["_id"])}))
    fields = [f.get("id") for f in form_doc.get("fields", [])]

    def iter_rows():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp"] + fields)
        yield output.getvalue(); output.seek(0); output.truncate(0)
        for s in subs:
            row = [s.get("created_at").isoformat() if s.get("created_at") else ""]
            for fid in fields:
                val = s.get("data", {}).get(fid)
                if isinstance(val, list):
                    val = ", ".join(map(str, val))
                row.append(val)
            writer.writerow(row)
            yield output.getvalue(); output.seek(0); output.truncate(0)
    return StreamingResponse(iter_rows(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={slug}.csv"})


@app.get("/api/forms/{slug}/qr")
def form_qr(slug: str):
    url = f"{PUBLIC_BASE_URL.rstrip('/')}/f/{slug}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")
