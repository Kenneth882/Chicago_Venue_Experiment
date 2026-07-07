"""Stage 2 cheap filter gate — Work order 4 (Milestone 3). Not yet implemented.

CLI: python -m src.stage2_filter [--limit N] [--dry-run]. Ordered checks on
stage 1_geo_ok rows: operational -> rating/review floor -> price level ->
website present -> HTTP 200 -> identity token-overlap score. Every kill gets
a reason code; middle-band identity scores go to needs_review (no LLM yet).
"""
