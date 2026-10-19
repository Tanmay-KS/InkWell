"""Persistent household memory.

Phase 1 (build) → Phase 3 (updates) — Person B.

Two distinct things live here, and keeping them separate is the whole point:

* **The household picture** — who is in this household, what they are
  subscribed to, which accounts exist. Facts that are true until something
  changes them.
* **The handled log** — what we have already told the user about, and which
  drafts we already produced. This is what stops the agent from flagging the
  same renewal every morning.

Phase 3 adds the third thing: facts that *change*. "I cancelled that plan in
March" has to supersede "I have a plan", not sit next to it. `update_tracker.py`
is where that supersession is recorded.

The store is Hermes's own memory system (`MEMORY.md`, `USER.md`, FTS5
cross-session recall) plus a structured sidecar for the parts that need to
be queryable. See `docs/architecture.md` for why the structured sidecar
exists.
"""

__all__ = ["hermes_memory", "update_tracker"]
