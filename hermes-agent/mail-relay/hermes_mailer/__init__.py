"""hermes-mailer — privilege-separated email broker.

See PROTOCOL.md for the design contract and threat model.

This package is consumed in two contexts:
  - the daemon process (`hermes_mailer.daemon`) running under a transient
    DynamicUser systemd user;
  - tests (which exercise modules in-process).

The plugin client (`hermes_mailer.client`) is a thin UDS client that runs
inside Elena's runtime — it does NOT import the policy modules.
"""

__version__ = "0.1.0"
