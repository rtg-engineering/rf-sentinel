"""RF Sentinel Analyst: read-only, evidence-backed investigation over captured API snapshots.

The analyst never touches ingestion or detection state. It reads snapshots of the normal
``/api/status`` response, exposes them through allowlisted read-only tools, and produces
reports whose every factual claim is mechanically checked against the cited evidence.
"""

SCHEMA_VERSION = "1.0"
