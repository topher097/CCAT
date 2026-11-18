"""Persistence layer for the CCAT refactor (plan §4).

SQLAlchemy 2.x declarative models, engine/session helpers, Alembic
migrations, and bootstrap seeding.  SQLite is the active database
today; every construct here stays portable to PostgreSQL (plan §4.5:
portable column types, named constraints, no SQLite-only SQL).

Two deliberate renames versus the legacy workbook schemas, applied
only here; Phase 2b repositories map at the storage boundary so no
other layer sees the legacy spellings:

- Child ``potential_anomoly`` → :attr:`FinalChild.ai_screening_notes`
  (plan §1.2 rule 10; legacy workbook column and legacy AI JSON key
  are translated by the import/export adapters).
- Sponsor legacy ``create`` → :attr:`FinalSponsor.created` (plan §4.3;
  the importer/exporter maps the legacy spelling explicitly).
"""
