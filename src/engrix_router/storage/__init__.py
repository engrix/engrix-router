"""Persistence layer: SQLite connection/transaction primitives and stored state.

Holds the things that live in tables: schema bootstrap, the runtime settings
overlay, API-key records are NOT here (they are identity/). Lower layers never
import from here.
"""
