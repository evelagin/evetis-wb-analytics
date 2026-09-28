"""EVETIS Reviews & Q&A v3 — structured knowledge, policy engine, verifier, shadow mode.

v3 is SHADOW-ONLY: nothing in this package talks to Wildberries or Telegram. The
production answer, card and publication path stay on v2 until an explicit owner
cutover decision. Enforced by tests (``tests/v3/test_isolation.py``).
"""

ENGINE_VERSION = "v3.0.0-shadow"
V3_CUSTOMER_FACING = False  # hard invariant for Phase 3
