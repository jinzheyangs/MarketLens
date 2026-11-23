"""ORM models. Importing this package registers metadata for Alembic."""

from app.db.models.tenant import Tenant

__all__ = ["Tenant"]
