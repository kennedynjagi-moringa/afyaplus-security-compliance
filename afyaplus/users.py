"""Demo user store.

Only bcrypt hashes live in the repository. In production this module would be
replaced by the identity provider; the rest of the service only sees `User`.

Week 9: what a role may do is decided by security/rbac.py (default deny). A user's token scopes are derived
from that matrix, so the Week 6 scopes and the Week 9 matrix cannot drift apart.

| username          | role             | demo password         | added   |
|-------------------|------------------|-----------------------|---------|
| chw_amina         | chw              | amina-chw-2026        | Week 6  |
| dispatcher_otieno | dispatcher       | otieno-dispatch-2026  | Week 6  |
| auditor_wanjiru   | auditor          | wanjiru-audit-2026    | Week 6  |
| pharm_kisumu      | partner_pharmacy | kisumu-pharm-2026     | Week 9  |
| ops_kamau         | ops              | kamau-ops-2026        | Week 9  |
| dpo_njeri         | dpo              | njeri-dpo-2026        | Week 9  |
| contractor_temp   | contractor       | temp-contractor-2026  | Week 9  |

contractor_temp has a role nobody added to the matrix, so every route answers 403.
"""

from __future__ import annotations

from dataclasses import dataclass

import bcrypt

from security.rbac import scopes_for


@dataclass(frozen=True)
class User:
    username: str
    role: str
    password_hash: str = ""

    @property
    def scopes(self) -> tuple[str, ...]:
        return scopes_for(self.role)


_USERS: dict[str, User] = {
    u.username: u
    for u in (
        User("chw_amina", "chw", "$2b$12$bkYhu4FhcbG1jMzZwm.mgeMp..JRhA96irJv3HnFwsvGr0RGySwFC"),
        User("dispatcher_otieno", "dispatcher", "$2b$12$ye0IDs0KnTyJXsSQs3nKquJfywXx5sLqpe2e54t5nb3.ojPAhqesm"),
        User("auditor_wanjiru", "auditor", "$2b$12$PvaM4UuBvetZQq0IP0On2O4KHazity7F3ZvZjPDvLjGcoM8sRbMcK"),
        User("pharm_kisumu", "partner_pharmacy", "$2b$12$E5vw0tllWyT62OCP9yDX.O9Z9KNHx9dTMEomrEZSnER4w92rjxpza"),
        User("ops_kamau", "ops", "$2b$12$zuGZZ77aIY.PfWiEkjB6zO8CHqvve5l5xMKcnB7io.0C0x6Ia43Ei"),
        User("dpo_njeri", "dpo", "$2b$12$4MMNun07EN0I2xkIinNDguKhRi/QM9.yqf4uyyUtdIG8nkrxv9WAC"),
        User("contractor_temp", "contractor", "$2b$12$4o3uP7ZR.Wf.EcGX533/bueluBidMWHx1BxfUFevKfqpwPUb1/7L6"),
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
