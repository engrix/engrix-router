"""An adapter that explodes on import -- fixture for the discovery tests.

This is the failure mode ADR-0002 has to survive: one private provider package
broken (missing dependency, syntax error after a vendor change, whatever) must not
blank out every other provider in the dashboard, and must not be silent either.
"""
raise RuntimeError("this adapter is broken on purpose")
