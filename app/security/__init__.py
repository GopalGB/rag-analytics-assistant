"""Deterministic safety layer: input firewall, output scrubber, and system-prompt armor.

This layer runs BEFORE any retrieval or model call (input) and AFTER generation (output).
It does not depend on the LLM, so safety holds even when a model misbehaves.
"""

from app.security.guardrails import Decision, InputGuard
from app.security.output_filter import scrub
from app.security.system_prompt import SECURITY_ARMOR, build_system_prompt

__all__ = ["Decision", "InputGuard", "scrub", "SECURITY_ARMOR", "build_system_prompt"]
