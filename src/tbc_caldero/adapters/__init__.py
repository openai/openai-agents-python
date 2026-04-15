"""
TBC Adapters — thin bridges to external TBC systems (DW, Neo4j, Granola, etc.).

Adapters are NOT capabilities. They are low-level clients that capabilities
call to get real data. Keeping them separate preserves the thin-harness
discipline: the Caldero kernel knows nothing about TBC, capabilities know
about TBC semantics, and adapters translate between them.
"""
