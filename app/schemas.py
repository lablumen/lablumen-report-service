import datetime
import uuid

from pydantic import BaseModel


class ReportViewOut(BaseModel):
    url: str
    expires_in: int


_FAILED_SENTINEL = "__failed__"


class ReportListItem(BaseModel):
    report_id: uuid.UUID
    test_name: str
    patient_name: str
    created_at: datetime.datetime
    has_summary: bool
    summary: str | None = None
    processing_failed: bool = False


class ReportUploadOut(BaseModel):
    report_id: uuid.UUID
    status: str


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    question: str
    history: list[ChatTurn] = []


class ChatResponse(BaseModel):
    answer: str
    disclaimer: str
