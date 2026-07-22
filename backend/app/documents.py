from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.models import Document
from app.models.document import DocumentStatus
from app.permissions import (
    AuthContext,
    Permission,
    Principal,
    current_principal,
    require,
    resolve_role,
)
from app.storage import head_object, presign_get, presign_put

router = APIRouter(tags=["documents"])

Session = Annotated[AsyncSession, Depends(get_session)]

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_SIZE_BYTES = 10 * 1024 * 1024


class DocumentCreateIn(BaseModel):
    filename: str = Field(min_length=1)
    content_type: str
    size_bytes: int = Field(gt=0)


class DocumentCreateOut(BaseModel):
    document_id: UUID
    upload_url: str
    headers: dict[str, str]


class DocumentOut(BaseModel):
    id: UUID
    house_id: UUID
    content_type: str
    size_bytes: int
    status: DocumentStatus


@router.post("/houses/{house_id}/documents", status_code=201)
async def create_document(
    body: DocumentCreateIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.DOCUMENT_UPLOAD))],
    session: Session,
) -> DocumentCreateOut:
    if body.content_type not in ALLOWED_CONTENT_TYPES:
        raise ApiError(422, "VALIDATION_ERROR", f"Unsupported content_type: {body.content_type}")
    if body.size_bytes > MAX_SIZE_BYTES:
        raise ApiError(422, "VALIDATION_ERROR", "File exceeds 10 MB limit")

    document = Document(
        house_id=ctx.house_id,
        uploaded_by=ctx.principal.user_id,
        r2_key="",  # set below once we have the id
        content_type=body.content_type,
        size_bytes=body.size_bytes,
    )
    session.add(document)
    await session.flush()
    document.r2_key = f"house/{ctx.house_id}/doc/{document.id}"
    await session.commit()

    url, headers = presign_put(document.r2_key, body.content_type, body.size_bytes)
    return DocumentCreateOut(document_id=document.id, upload_url=url, headers=headers)


@router.post("/documents/{document_id}/confirm")
async def confirm_document(
    document_id: UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Session,
) -> DocumentOut:
    document = await session.get(Document, document_id)
    if document is None:
        raise ApiError(404, "NOT_FOUND", "Document not found")
    if await resolve_role(session, principal.user_id, document.house_id) is None:
        raise ApiError(403, "PERMISSION_DENIED", "Not a member of this document's house")

    exists, size = await head_object(document.r2_key)
    if not exists or size != document.size_bytes:
        raise ApiError(409, "CONFLICT", "Uploaded object not found or size mismatch")

    document.status = DocumentStatus.uploaded
    await session.commit()
    return DocumentOut(
        id=document.id, house_id=document.house_id,
        content_type=document.content_type, size_bytes=document.size_bytes,
        status=document.status,
    )


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Session,
) -> RedirectResponse:
    document = await session.get(Document, document_id)
    if document is None:
        raise ApiError(404, "NOT_FOUND", "Document not found")
    if await resolve_role(session, principal.user_id, document.house_id) is None:
        raise ApiError(403, "PERMISSION_DENIED", "Not a member of this document's house")

    return RedirectResponse(url=presign_get(document.r2_key), status_code=302)
