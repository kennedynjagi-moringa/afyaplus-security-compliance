"""Demo user store.

Only bcrypt hashes live in the repository. In production this module would be
replaced by the identity provider; the rest of the service only sees `User`.

| username          | role       | scopes                   | demo password         |
|-------------------|------------|--------------------------|-----------------------|
| chw_amina         | chw        | triage:write, agent:ask  | amina-chw-2026        |
| dispatcher_otieno | dispatcher | agent:ask                | otieno-dispatch-2026  |
| auditor_wanjiru   | auditor    | audit:read               | wanjiru-audit-2026    |
"""

from __future__ import annotations

from dataclasses import dataclass

import bcrypt


@dataclass(frozen=True)
class User:
    username: str
    role: str
    scopes: tuple[str, ...]
    password_hash: str = ""


_USERS: dict[str, User] = {
    u.username: u
    for u in (
        User("chw_amina", "chw", ("triage:write", "agent:ask"),
             "$2b$12$bkYhu4FhcbG1jMzZwm.mgeMp..JRhA96irJv3HnFwsvGr0RGySwFC"),
        User("dispatcher_otieno", "dispatcher", ("agent:ask",),
             "$2b$12$ye0IDs0KnTyJXsSQs3nKquJfywXx5sLqpe2e54t5nb3.ojPAhqesm"),
        User("auditor_wanjiru", "auditor", ("audit:read",),
             "$2b$12$PvaM4UuBvetZQq0IP0On2O4KHazity7F3ZvZjPDvLjGcoM8sRbMcK"),
    )
}

# Checked when the username is unknown, so a failed login costs the same time
# whether or not the account exists (no user enumeration by timing).
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=12))


def authenticate(username: str, password: str) -> User | None:
    user = _USERS.get(username)
    candidate = user.password_hash.encode() if user else _DUMMY_HASH
    ok = bcrypt.checkpw(password.encode(), candidate)
    return user if (user and ok) else None


def get_user(username: str) -> User | None:
    return _USERS.get(username)
