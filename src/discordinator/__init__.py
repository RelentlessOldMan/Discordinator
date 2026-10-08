"""Discordinator — CLI + MCP server for relaying messages through Discord channels."""

__version__ = "1.0.37"


def use_system_certs() -> None:
    """Verify TLS against the OS trust store.

    Lets corporate TLS-inspection CAs (already in the OS store) validate without
    configuring SSL_CERT_FILE — the same check a browser does. Falls back to
    Python's bundled CA list if truststore is unavailable. Call once at process
    start (from an entry point), not at import time.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass
