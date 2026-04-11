"""SQLAlchemy ORM models.

Import every model module here so Alembic autogenerate sees them when it
inspects `Base.metadata`.
"""

from app.models.customer import Customer, CustomerContact  # noqa: F401
from app.models.order import (  # noqa: F401
    Order,
    OrderComment,
    OrderItem,
    OrderStatusHistory,
)
from app.models.tenant import Tenant  # noqa: F401
from app.models.user import User  # noqa: F401
