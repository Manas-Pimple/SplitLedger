"""Phase 10 done-gates against the real dev MinIO (localhost:9002):
presign -> PUT -> confirm -> download redirect works end-to-end; non-member
download -> 403; oversized presign request -> 422.
"""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models import Document
from app.models.house import MembershipRole
from app.scheduler import _sweep_orphan_documents
from app.storage import get_s3_client, head_object
from tests.factories import make_house, make_membership, make_user
from tests.helpers import auth


async def _setup(session: AsyncSession):  # type: ignore[no-untyped-def]
    house = await make_house(session)
    manager = await make_user(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    await session.commit()
    return house, manager


async def test_presign_put_confirm_download_roundtrip(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager = await _setup(session)
    body = b"not a real jpeg, just test bytes"

    r = await client.post(
        f"/api/v1/houses/{house.id}/documents",
        json={"filename": "receipt.jpg", "content_type": "image/jpeg", "size_bytes": len(body)},
        headers=auth(manager),
    )
    assert r.status_code == 201, r.text
    presign = r.json()
    document_id = presign["document_id"]

    # actual PUT of bytes to MinIO via the presigned URL — no mocking
    async with httpx.AsyncClient() as raw:
        put = await raw.put(presign["upload_url"], content=body, headers=presign["headers"])
        assert put.status_code in (200, 204), put.text

    r = await client.post(
        f"/api/v1/documents/{document_id}/confirm",
        headers=auth(manager),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "uploaded"

    r = await client.get(
        f"/api/v1/documents/{document_id}/download",
        headers=auth(manager),
        follow_redirects=False,
    )
    assert r.status_code == 302
    download_url = r.headers["location"]

    async with httpx.AsyncClient() as raw:
        fetched = await raw.get(download_url)
        assert fetched.status_code == 200
        assert fetched.content == body


async def test_confirm_without_upload_is_conflict(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager = await _setup(session)
    document_id = (
        await client.post(
            f"/api/v1/houses/{house.id}/documents",
            json={"filename": "x.png", "content_type": "image/png", "size_bytes": 1000},
            headers=auth(manager),
        )
    ).json()["document_id"]

    r = await client.post(f"/api/v1/documents/{document_id}/confirm", headers=auth(manager))
    assert r.status_code == 409


async def test_download_denied_for_non_member(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager = await _setup(session)
    stranger = await make_user(session)
    await session.commit()

    document_id = (
        await client.post(
            f"/api/v1/houses/{house.id}/documents",
            json={"filename": "x.pdf", "content_type": "application/pdf", "size_bytes": 500},
            headers=auth(manager),
        )
    ).json()["document_id"]

    r = await client.get(
        f"/api/v1/documents/{document_id}/download",
        headers=auth(stranger),
        follow_redirects=False,
    )
    assert r.status_code == 403


async def test_oversized_and_bad_content_type_rejected(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager = await _setup(session)

    r = await client.post(
        f"/api/v1/houses/{house.id}/documents",
        json={
            "filename": "huge.pdf",
            "content_type": "application/pdf",
            "size_bytes": 11 * 1024 * 1024,
        },
        headers=auth(manager),
    )
    assert r.status_code == 422

    r = await client.post(
        f"/api/v1/houses/{house.id}/documents",
        json={"filename": "x.exe", "content_type": "application/x-msdownload", "size_bytes": 10},
        headers=auth(manager),
    )
    assert r.status_code == 422


async def test_orphan_sweep_deletes_row_and_object(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    house, manager = await _setup(session)
    key = f"house/{house.id}/doc/orphan-test-{house.id}"
    body = b"orphaned bytes"
    get_s3_client().put_object(Bucket=get_settings().r2_bucket, Key=key, Body=body)

    doc = Document(
        house_id=house.id, uploaded_by=manager.id, r2_key=key,
        content_type="image/png", size_bytes=len(body),
    )
    session.add(doc)
    await session.flush()
    doc_id = doc.id
    await session.execute(
        text("UPDATE documents SET created_at = :t WHERE id = :id"),
        {"t": datetime.now(UTC) - timedelta(hours=25), "id": doc_id},
    )
    await session.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _sweep_orphan_documents(factory)

    session.expire_all()
    assert await session.get(Document, doc_id) is None
    exists, _ = await head_object(key)
    assert exists is False
