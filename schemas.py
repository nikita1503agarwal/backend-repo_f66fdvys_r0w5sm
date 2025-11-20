"""
Database Schemas for SmartForm Builder

Each Pydantic model corresponds to a MongoDB collection (lowercased class name).
- Form -> "form"
- Submission -> "submission"
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class FormFieldOption(BaseModel):
    label: str
    value: str

class FormField(BaseModel):
    id: str
    type: str = Field(..., description="text, email, phone, number, date, file, dropdown, checkbox, radio, signature, textarea")
    label: str
    placeholder: Optional[str] = None
    required: bool = False
    options: Optional[List[FormFieldOption]] = None
    helperText: Optional[str] = None

class Form(BaseModel):
    title: str
    description: Optional[str] = None
    fields: List[FormField]
    sheet_id: Optional[str] = None
    sheet_name: Optional[str] = None
    share_slug: Optional[str] = None
    owner_uid: Optional[str] = None

class Submission(BaseModel):
    form_id: str
    data: Dict[str, Any]
    file_links: Optional[Dict[str, str]] = None
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
