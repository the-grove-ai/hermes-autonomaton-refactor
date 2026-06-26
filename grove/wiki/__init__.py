"""grove.wiki — the living cellar (Sprint K1, living-cellar-v1).

A compaction pipeline that turns Fleet skill output (and operator-curated
docs) into canonical, BM25-searchable knowledge pages under
``$GROVE_WIKI_PATH``, with lazy auto-ingest and a CLI. Not hot-path: nothing
in this package registers against the PromptComposer in K1.
"""
