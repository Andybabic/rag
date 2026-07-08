"""Authentication: user accounts and password hashing.

Public API::

    from shared.auth import verify_user, create_user, list_users
    from shared.auth import ensure_bootstrap_admin
    from shared.auth import hash_password, verify_password
"""

from shared.auth.passwords import hash_password, verify_password
from shared.auth.users import (
    User,
    count_users,
    create_user,
    delete_user,
    ensure_bootstrap_admin,
    get_user,
    list_user_use_cases,
    list_users,
    set_password,
    set_role,
    set_user_use_cases,
    verify_user,
)

__all__ = [
    "User",
    "count_users",
    "create_user",
    "delete_user",
    "ensure_bootstrap_admin",
    "get_user",
    "hash_password",
    "list_user_use_cases",
    "list_users",
    "set_password",
    "set_role",
    "set_user_use_cases",
    "verify_password",
    "verify_user",
]
