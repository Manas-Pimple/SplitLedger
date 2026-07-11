from uuid import UUID, uuid4

from app.models import User
from app.security import create_access_token


def auth(user: User | UUID) -> dict[str, str]:
    """Auth + fresh idempotency headers for endpoint tests. Accepts a User or a
    raw id (useful once ORM objects are expired)."""
    user_id = user if isinstance(user, UUID) else user.id
    return {
        "Authorization": f"Bearer {create_access_token(user_id)}",
        "Idempotency-Key": str(uuid4()),
    }
