"""Version 2 storage primitives.

The V2 storage layer is deliberately small and boring: a deterministic
workspace layout, one SQLite connection policy, explicit transactions, and
schema bootstrap helpers.  Higher-level services own business invariants.
"""

from .database import (
    DatabaseError,
    SQLiteDatabase,
    connect_database,
    open_database,
    execute_sql_script,
)
from .layout import LayoutError, WorkspaceV2Layout
from .schema import (
    DOMAIN_SCHEMAS,
    SCHEMA_MARKER,
    SCHEMA_VERSION,
    SchemaError,
    initialize_all,
    initialize_database,
    initialize_domain,
    initialize,
    bootstrap,
)
from .transaction import (
    Transaction,
    TransactionError,
    atomic,
    begin_immediate,
    begin_transaction,
    transaction,
)

__all__ = [
    "DatabaseError",
    "DOMAIN_SCHEMAS",
    "SCHEMA_MARKER",
    "SCHEMA_VERSION",
    "SchemaError",
    "SQLiteDatabase",
    "Transaction",
    "TransactionError",
    "WorkspaceV2Layout",
    "LayoutError",
    "connect_database",
    "atomic",
    "begin_immediate",
    "begin_transaction",
    "execute_sql_script",
    "initialize_all",
    "initialize",
    "bootstrap",
    "initialize_database",
    "initialize_domain",
    "open_database",
    "transaction",
]
