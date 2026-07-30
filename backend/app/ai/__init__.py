"""AI subsystem.

Layered with one-way dependencies, so each layer can be replaced without touching
the others:

    parsers -> cdm -> enrichment -> classification -> profiles
            -> chunking -> extraction -> embedding -> graph
            -> retrieval -> context -> prompt -> rag

Nothing in a lower layer imports from a higher one.
"""
