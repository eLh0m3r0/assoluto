"""SQLAlchemy ORM models.

Import every model module here so Alembic autogenerate sees them when it
inspects `Base.metadata`.
"""

from app.models.asset import Asset, AssetMovement  # noqa: F401
from app.models.attachment import OrderAttachment  # noqa: F401
from app.models.customer import Customer, CustomerContact  # noqa: F401
from app.models.order import (  # noqa: F401
    Order,
    OrderComment,
    OrderItem,
    OrderStatusHistory,
)
from app.models.product import Product  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.user import User  # noqa: F401

# Platform (SaaS) models. Imported eagerly so Alembic's metadata sees
# them when generating migrations. The tables exist in every database
# regardless of the FEATURE_PLATFORM flag; leaving them empty is a
# no-op for open-source self-host deployments that never `install()`
# the platform package.
from app.platform.models import Identity, TenantMembership  # noqa: F401
