"""Jidoka's detectors — the watchers.

A detector observes the feed and, when it sees something, calls
``grove.andon.raise_andon`` and returns. That is all a detector does. It
holds no reference to Kaizen, retries nothing and falls back to nothing; what
happens after the cord is pulled is the handler's job, identically for every
detector. (A test fails the build if any module in this package imports
Kaizen.)
"""
