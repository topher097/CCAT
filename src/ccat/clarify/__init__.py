"""Module 7 — clarification email tool.

Reads the Angels whose ``final_child`` rows carry a
``clarification_notes`` note, groups them by family, and generates one
polite clarification email per family (prose written by an LLM via the
opencode SDK, wrapped in a paste-safe HTML template) addressed to the
family's primary email.  The drafts are edited through the web
application (:mod:`ccat.api` plus the Vue SPA); this package holds the
generation logic behind it.
"""

from ccat.clarify.extract import ClarifyError

__all__ = ["ClarifyError"]
