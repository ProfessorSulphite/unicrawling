"""
Everything that turns a university URL into a validated payload.

Not a pipeline phase -- a purpose. Split into linkers/ (find the right pages),
crawlers/ (query NotebookLM and repair its output) and normalizers/ (canonicalise
the result).
"""
