"""Module 7 — clarification email tool.

Reads the Angels whose ``final_child`` rows carry a
``clarification_notes`` note, groups them by family, and serves a
local NiceGUI web app where the operator generates one polite
clarification email per family (prose written by an LLM via the
opencode SDK, wrapped in a paste-safe HTML template) addressed to the
family's primary email.
"""

from ccat.clarify.extract import ClarifyError

__all__ = ["ClarifyError"]
