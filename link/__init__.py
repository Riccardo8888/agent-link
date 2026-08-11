"""agent-link: an encrypted channel between coding agents, anywhere.

One dependency on the agent side (`cryptography`); the relay has none beyond
the standard library.
"""

__version__ = "2.2.0"

# Governs the peer wire: the sealed envelope both ends must agree on. The
# control socket between the MCP server and the daemon is versioned separately
# by __version__, because that is the one that breaks on upgrade day.
PROTOCOL_VERSION = 2
