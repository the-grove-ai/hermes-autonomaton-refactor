"""Sovereign-config writers and loaders for the Grove Autonomaton.

This package holds the sanctioned mutators and readers for the operator's
runtime config under ``~/.grove`` — currently the routing-config writer
(``routing_writer``) and the model catalog loader (``model_catalog``). The
read paths elsewhere in the codebase stay on ``yaml.safe_load``; anything
that WRITES a comment-bearing operator file funnels through here so the
ruamel round-trip lives in one place.
"""
