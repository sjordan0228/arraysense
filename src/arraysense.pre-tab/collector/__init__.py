"""arraysense/collector/__init__.py — where readings come from: sources and the poll loop.

Everything that knows about the wire lives behind the source interface. The
dongle's TCP port is being removed in newer firmware, so the transport has to be
replaceable without the store or the API noticing that it changed.
"""
