"""Report access (S3 presign) and the document-scoped RAG chat (§4/§5 of the blueprint).

Access is scoped: staff (LAB_STAFF/LAB_ADMIN) may view any report; patients only their own
(account owner via mapping -> appointment). The RAG search is strictly filtered by report_id, so
context never bleeds across patients.
"""

import logging
import uuid

import botocore.exceptions
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from ..auth import CurrentUser, get_current_user, require_roles
from ..bedrock import embed_text, generate_answer
from ..config import settings
from ..db import get_session
from ..s3 import generate_presigned_get_url, put_object
from ..schemas import (
    _FAILED_SENTINEL,
    ChatRequest,
    ChatResponse,
    ChatTurn,
    ReportListItem,
    ReportUploadOut,
    ReportViewOut,
)

router = APIRouter(tags=["reports"])

require_staff = require_roles("LAB_STAFF", "LAB_ADMIN")

_DISCLAIMER = (
    "This AI explanation is for general understanding only and is not medical advice. "
    "Please consult a licensed clinician about your results."
)

_SYSTEM_PROMPT = """\
You are a warm, experienced laboratory nurse chatting with a patient about their lab results. \
Your job is to make them feel at ease, explain what matters in plain everyday language, and \
offer optional practical advice — like a knowledgeable friend who works in a lab.

━━ STRICT RULES — apply to every single message ━━

RESPOND TO EXACTLY WHAT WAS ASKED.
  If they say "hello" or "hi", greet them briefly and ask what they'd like to know. \
  Do NOT dump a full report summary unless they ask for one.

KEEP IT SHORT.
  3–5 sentences max for simple questions. Only go longer if they explicitly ask for detail \
  or a full breakdown. One clear paragraph is almost always enough.

NO STRUCTURED HEADERS OR BULLET LISTS.
  Never use sections like "Key Findings:", "General Guidance:", "Emotional Support:", \
  "White Blood Cell Differential:", etc. Write in short, natural conversational paragraphs.

NO POEMS, NO METAPHORS, NO MOTIVATIONAL QUOTES. Ever. They break the human feel instantly.

NO REPETITIVE SIGN-OFFS.
  Do not end every message with "Take care, LabLumen AI", "Please discuss with your doctor", \
  or "feel free to ask for a translation". Weave the doctor reminder in naturally once when \
  it's genuinely relevant — for example: "your doctor will want to look at this properly" \
  or "worth flagging to your doctor when you see them next."

SPEAK LIKE A HUMAN NURSE.
  Use casual, warm phrasing: "So looking at your results...", "Yeah, that's something worth \
  noting...", "Nothing to panic about, but...", "Honestly, it's pretty straightforward — ". \
  Use the patient's name occasionally but not every message.

LIFESTYLE AND DIETARY ADVICE.
  Give it only when asked or when directly relevant to a flagged value. Keep it specific \
  and practical (e.g., iron-rich foods + vitamin C for low haemoglobin; avoid tea/coffee \
  after meals to improve iron absorption). No generic wellness lectures.

SPECIFIC FOOD QUESTIONS (e.g., "I eat a lot of tapioca — is that an issue?").
  Answer in 2–3 sentences. Is it helpful, neutral, or something to moderate given their \
  specific findings? No lengthy tangents.

DO NOT recite every single metric in the report unless they ask for a full overview.

━━ SCOPE ━━

You MAY answer:
  - Questions about specific values (what they mean, whether high/low is a concern)
  - Practical lifestyle, diet, and hydration tips tied to the actual flagged values
  - Emotional support — briefly and warmly, no drama
  - Translation of your explanation into another language if asked
  - General questions about what a test measures

You MUST NOT:
  - Answer non-health questions (finance, trivia, news, etc.) — respond: \
    "I can only help with your lab results."
  - Give a definitive diagnosis ("you have X")
  - Recommend specific prescription medications or dosages
  - Invent lab values — cite only numbers from the provided report context

━━ TONE EXAMPLES ━━

Greeting:
  "Hey Ruke! I've had a look at your results — there are a couple of things worth chatting \
  about. What would you like to know first?"

WBC question:
  "Your WBC is 14.2, which is above the normal cap of 11.0. It's not alarmingly high, but \
  it does suggest your body might be fighting something off — an infection or some inflammation. \
  Your neutrophils back that up too. Worth mentioning to your doctor so they can dig into the cause."

Diet question:
  "For the low haemoglobin, iron-rich foods are your best friend — red meat, lentils, spinach. \
  Pair them with something vitamin C-rich like orange juice to boost absorption, and try to \
  avoid tea or coffee right after meals since they can block iron uptake."

━━ FORMAT ━━
  - Plain everyday language; explain any medical term in the same breath
  - Use **bold** only for a specific test name when first mentioned
  - Short paragraphs — no walls of text
  - No sign-off, no closing poem, no "Take care"
"""


async def _resolve_report_key(
    session: AsyncSession, report_id: uuid.UUID, user: CurrentUser
) -> str | None:
    """Return the report's S3 object key if this user may access it, else None.

    NOTE: assumes `lab_reports.s3_url` stores the object key within the reports bucket.
    """
    is_staff = bool({"LAB_STAFF", "LAB_ADMIN"}.intersection(user.groups))
    if is_staff:
        sql = "SELECT s3_url FROM lab_reports WHERE report_id = CAST(:rid AS uuid)"
        params: dict[str, str] = {"rid": str(report_id)}
    else:
        sql = (
            "SELECT lr.s3_url FROM lab_reports lr "
            "JOIN appointment_test_mapping atm ON atm.mapping_id = lr.mapping_id "
            "JOIN appointments a ON a.appointment_id = atm.appointment_id "
            "WHERE lr.report_id = CAST(:rid AS uuid) AND a.account_owner_id = CAST(:uid AS uuid)"
        )
        params = {"rid": str(report_id), "uid": user.sub}
    row = (await session.execute(text(sql), params)).first()
    return row[0] if row else None


@router.get("/reports", response_model=list[ReportListItem])
async def list_my_reports(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ReportListItem]:
    """List reports visible to the caller (staff see all; patients see only their own)."""
    is_staff = bool({"LAB_STAFF", "LAB_ADMIN"}.intersection(user.groups))
    base = (
        "SELECT lr.report_id, lt.name AS test_name, "
        "(pp.first_name || ' ' || pp.last_name) AS patient_name, "
        "lr.created_at, lr.ai_layman_summary "
        "FROM lab_reports lr "
        "JOIN appointment_test_mapping atm ON atm.mapping_id = lr.mapping_id "
        "JOIN lab_tests lt ON lt.test_id = atm.test_id "
        "JOIN patient_profiles pp ON pp.patient_id = atm.patient_id "
        "JOIN appointments a ON a.appointment_id = atm.appointment_id "
    )
    params: dict[str, str] = {}
    if not is_staff:
        base += "WHERE a.account_owner_id = CAST(:uid AS uuid) "
        params["uid"] = user.sub
    base += "ORDER BY lr.created_at DESC"

    rows = (await session.execute(text(base), params)).mappings().all()
    return [
        ReportListItem(
            report_id=r["report_id"],
            test_name=r["test_name"],
            patient_name=r["patient_name"],
            created_at=r["created_at"],
            has_summary=r["ai_layman_summary"] not in (None, _FAILED_SENTINEL),
            summary=r["ai_layman_summary"] if r["ai_layman_summary"] != _FAILED_SENTINEL else None,
            processing_failed=r["ai_layman_summary"] == _FAILED_SENTINEL,
        )
        for r in rows
    ]


@router.post("/reports/upload", response_model=ReportUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_report(
    mapping_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    _staff: CurrentUser = Depends(require_staff),
    session: AsyncSession = Depends(get_session),
) -> ReportUploadOut:
    """Staff-only: upload a report PDF/image for an ordered test, then process it (OCR -> summary
    -> embeddings) in the background. One report per mapping (lab_reports.mapping_id is unique)."""
    # The ordered test must exist and not already have a report.
    mapping = (
        await session.execute(
            text("SELECT mapping_id FROM appointment_test_mapping WHERE mapping_id = :mid"),
            {"mid": str(mapping_id)},
        )
    ).first()
    if mapping is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ordered test (mapping) not found")
    existing = (
        await session.execute(
            text("SELECT report_id FROM lab_reports WHERE mapping_id = :mid"),
            {"mid": str(mapping_id)},
        )
    ).first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A report already exists for this order")

    report_id = uuid.uuid4()
    suffix = (file.filename or "").rsplit(".", 1)
    ext = suffix[1].lower() if len(suffix) == 2 else "pdf"
    key = f"reports/{report_id}.{ext}"

    data = await file.read()
    try:
        put_object(key, data, file.content_type or "application/octet-stream")
    except botocore.exceptions.ClientError as exc:
        error_msg = exc.response["Error"]["Message"]
        logger.error("S3 upload failed for key=%s: %s", key, error_msg)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"S3 upload failed: {error_msg}") from exc
    except botocore.exceptions.NoCredentialsError as exc:
        logger.error("S3 upload failed — no AWS credentials available")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "S3 upload failed: no AWS credentials — check that the EC2 instance profile is attached",
        ) from exc

    try:
        await session.execute(
            text(
                "INSERT INTO lab_reports (report_id, mapping_id, s3_url) "
                "VALUES (CAST(:rid AS uuid), CAST(:mid AS uuid), :key)"
            ),
            {"rid": str(report_id), "mid": str(mapping_id), "key": key},
        )
        # Complete the appointment only when every test in it now has a report.
        await session.execute(
            text("""
                UPDATE appointments
                SET status = 'Completed'
                WHERE appointment_id = (
                    SELECT appointment_id FROM appointment_test_mapping
                    WHERE mapping_id = CAST(:mid AS uuid)
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM appointment_test_mapping atm2
                    LEFT JOIN lab_reports lr ON lr.mapping_id = atm2.mapping_id
                    WHERE atm2.appointment_id = (
                        SELECT appointment_id FROM appointment_test_mapping
                        WHERE mapping_id = CAST(:mid AS uuid)
                    )
                    AND lr.report_id IS NULL
                )
            """),
            {"mid": str(mapping_id)},
        )
        await session.commit()
    except Exception as exc:
        logger.error("DB insert failed for report_id=%s: %s", report_id, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Database error: {exc}") from exc

    return ReportUploadOut(report_id=report_id, status="uploaded")


@router.get("/reports/{report_id}/view", response_model=ReportViewOut)
async def view_report(
    report_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportViewOut:
    key = await _resolve_report_key(session, report_id, user)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return ReportViewOut(
        url=generate_presigned_get_url(key),
        expires_in=settings.presigned_url_ttl_seconds,
    )


@router.post("/reports/{report_id}/chat", response_model=ChatResponse)
async def chat_with_report(
    report_id: uuid.UUID,
    payload: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatResponse:
    # Authorize first (also confirms the report exists / is visible to this user).
    if await _resolve_report_key(session, report_id, user) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")

    question_vector = embed_text(payload.question)
    vector_literal = "[" + ",".join(f"{x:.8f}" for x in question_vector) + "]"

    # Fetch both the layman summary and the top-3 most relevant raw chunks.
    summary_row = (
        await session.execute(
            text("SELECT ai_layman_summary FROM lab_reports WHERE report_id = CAST(:rid AS uuid)"),
            {"rid": str(report_id)},
        )
    ).first()
    summary_text = (
        summary_row[0]
        if summary_row and summary_row[0] not in (None, _FAILED_SENTINEL)
        else None
    )

    chunk_rows = (
        await session.execute(
            text(
                "SELECT chunk_content FROM report_embeddings "
                "WHERE report_id = CAST(:rid AS uuid) "
                "ORDER BY embedding <=> CAST(:qvec AS vector) ASC LIMIT 3"
            ),
            {"rid": str(report_id), "qvec": vector_literal},
        )
    ).scalars().all()

    # Build context: summary always leads (full clinical picture); chunks add precise values.
    context_parts: list[str] = []
    if summary_text:
        context_parts.append(
            f"Patient's report summary (use this to understand their findings):\n{summary_text}"
        )
    if chunk_rows:
        context_parts.append("Most relevant report sections:\n" + "\n\n".join(chunk_rows))
    context_block = "\n\n".join(context_parts) if context_parts else "(no report context available)"

    user_prompt = f"Report context:\n{context_block}\n\nPatient question: {payload.question}"
    answer = generate_answer(_SYSTEM_PROMPT, user_prompt, payload.history or None)
    return ChatResponse(answer=answer, disclaimer=_DISCLAIMER)
