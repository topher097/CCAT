"""Idempotent bootstrap seeding (plan §5.2 auth, §8).

Creates the two development no-password staff accounts (decision D2),
plus the ``system`` and ``migration`` attribution identities used when
no human actor exists (plan §4.3: imported rows carry the migration
identity so attribution never becomes null).  Safe to run repeatedly.
"""

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ccat.db.models import User

#: (username, roles, dev_no_password) bootstrap set.
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
