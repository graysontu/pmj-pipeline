from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class RawJob(BaseModel):
    source_id: str
    source_name: str
    title: str
    company: str
    location: str
    description_html: str
    description_text: str
    apply_url: str
    date_posted: datetime
    job_type: str = "fulltime"
    remote_type: str  # "onsite", "remote", or "hybrid"
    company_url: Optional[str] = None
    company_logo_url: Optional[str] = None
