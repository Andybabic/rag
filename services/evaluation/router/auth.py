"""Auth API – login + user management.

These endpoints are reached only through the frontend proxy, which enforces
session auth and the admin role for user-management calls (see the SvelteKit
``hooks.server.ts``). Login itself is unauthenticated by design.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from shared.auth import (
    User,
    create_user,
    delete_user,
    list_user_use_cases,
    list_users,
    set_password,
    set_role,
    set_user_use_cases,
    verify_user,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


# ── schemas ───────────────────────────────────────────────────


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    created_at: str | None = None

    @classmethod
    def from_user(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            username=user.username,
            role=user.role,
            created_at=user.created_at,
        )


class CreateUserRequest(BaseModel):
    username: str
    password: str = Field(min_length=1)
    role: str = "user"


class UpdateUserRequest(BaseModel):
    password: str | None = None
    role: str | None = None


class UseCaseAssignment(BaseModel):
    use_cases: list[str] = Field(default_factory=list)


# ── login ─────────────────────────────────────────────────────


@router.post("/login", response_model=UserOut)
async def login(req: LoginRequest):
    """Verify credentials. Returns the user on success, 401 otherwise."""
    user = await verify_user(req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return UserOut.from_user(user)


# ── user management (admin-only, enforced at the frontend) ─────


@router.get("/users", response_model=list[UserOut])
async def get_users():
    users = await list_users()
    return [UserOut.from_user(u) for u in users]


@router.post("/users", response_model=UserOut, status_code=201)
async def post_user(req: CreateUserRequest):
    try:
        user = await create_user(req.username, req.password, req.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UserOut.from_user(user)


@router.patch("/users/{username}", response_model=UserOut)
async def patch_user(username: str, req: UpdateUserRequest):
    """Update a user's password and/or role."""
    changed = False
    try:
        if req.password is not None:
            if not await set_password(username, req.password):
                raise HTTPException(status_code=404, detail="User not found")
            changed = True
        if req.role is not None:
            if not await set_role(username, req.role):
                raise HTTPException(status_code=404, detail="User not found")
            changed = True
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=400, detail="Nothing to update")
    from shared.auth import get_user

    user = await get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserOut.from_user(user)


@router.get("/users/{username}/use-cases")
async def get_user_use_cases(username: str):
    """List the use-case ids a user is assigned to."""
    from shared.auth import get_user

    if await get_user(username) is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"use_cases": await list_user_use_cases(username)}


@router.put("/users/{username}/use-cases")
async def put_user_use_cases(username: str, req: UseCaseAssignment):
    """Replace a user's use-case assignments."""
    try:
        ok = await set_user_use_cases(username, req.use_cases)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"use_cases": await list_user_use_cases(username)}


@router.delete("/users/{username}", status_code=204)
async def remove_user(username: str):
    """Delete a user. Refuses to delete the last remaining admin."""
    users = await list_users()
    target = next((u for u in users if u.username == username), None)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    admins = [u for u in users if u.role == "admin"]
    if target.role == "admin" and len(admins) <= 1:
        raise HTTPException(
            status_code=400, detail="Cannot delete the last remaining admin"
        )
    await delete_user(username)
    return None
