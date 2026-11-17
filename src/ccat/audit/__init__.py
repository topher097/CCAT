"""Module 3 — multi-entity human audit tool (audit_architecture.md §8–§9).

Package layout (§9.1):

- ``store`` — pure data layer (no NiceGUI imports): dense adj schemas,
  status machine, duplicate groups, saves, output filters.
- ``migrate`` — one-time bootstrap of the new workbook from the old one.
- ``pages`` — NiceGUI pages + shared components.
- ``server`` — ``run()``: seed sheets, load state, ``ui.run``.
"""


class AuditError(Exception):
    """Raised when the audit tool cannot complete a request."""
