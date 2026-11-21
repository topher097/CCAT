"""Module 3 — multi-entity human audit data layer (audit_architecture.md §8–§9).

Package layout (§9.1):

- ``store`` — pure data layer: dense adj schemas, status machine,
  duplicate groups, saves, output filters.
- ``migrate`` — one-time bootstrap of the new workbook from the old one.

The interactive audit editors live in the web application
(:mod:`ccat.api` plus the Vue SPA); this package holds only the data
rules they read and write.
"""


class AuditError(Exception):
    """Raised when the audit tool cannot complete a request."""


class RowAuditedError(AuditError):
    """Raised when an audit would change the values of an audited row."""
