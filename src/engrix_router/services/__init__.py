"""
Background jobs that outlive a single request.

One layer above `accounts`/`providers`, below `pipeline`: a service may read accounts
and ask a provider for data, but it never decides routing and never serves a client.
"""
