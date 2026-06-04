"""hermes-greeninvoice — privilege-separated GreenInvoice (Morning) broker.

The daemon owns the GreenInvoice API key and enforces per-caller,
per-action rate limits. Hermes' greeninvoice plugin talks to it over a
Unix domain socket and never sees the credentials. See PROTOCOL.md for
the design contract.
"""
