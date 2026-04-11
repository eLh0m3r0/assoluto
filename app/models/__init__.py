"""SQLAlchemy ORM models.

Import every model module here so Alembic autogenerate sees them when it
inspects `Base.metadata`.
"""

from app.models.tenant import Tenant  # noqa: F401
