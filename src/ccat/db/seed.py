"""Idempotent bootstrap seeding (plan §5.2 auth, §8).

Creates the development no-password staff accounts (decision D2) — the
auto-login identity first — plus the ``system`` and ``migration``
attribution identities used when no human actor exists (plan §4.3:
imported rows carry the migration identity so attribution never becomes
null).  Safe to run repeatedly.
"""

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ccat.db.models import User

#: (username, roles, dev_no_password) bootstrap set.
#:
#: Roles are additive, not hierarchical: a route guarded by ``auditor``
#: needs that role, so the auto-login identity carries the same three
#: roles and can drive every workflow in development.
_BOOTSTRAP: tuple[tuple[str, list[str], bool], ...] = (
    ("cmendres", ["admin", "coordinator", "auditor"], True),
    ("lmboswell", ["coordinator", "auditor"], True),
    ("system", [], False),
    ("migration", [], False),
)


def seed_bootstrap(engine: Engine) -> None:
    """Create any missing bootstrap users; existing rows are untouched.

    Args:
        engine: A sync engine (from ``ccat.db.engine.create_sync_engine``)
            against a migrated database.
    """
    with Session(engine) as session:
        for username, roles, dev_no_password in _BOOTSTRAP:
            exists = session.scalar(
                select(User).where(User.username == username)
            )
            if exists is None:
                session.add(
                    User(
                        username=username,
                        roles=roles,
                        dev_no_password=dev_no_password,
                        active=True,
                    )
                )
        session.commit()
