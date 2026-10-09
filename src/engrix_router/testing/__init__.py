"""
Testing utilities the public core exports for provider packages.

`contract.ProviderContract` is the shared suite every provider -- in this repo or in a
private distribution -- is expected to run. It lives in the shipped package (not in
`tests/`) precisely so a separate distribution can import it from its own CI.
"""
