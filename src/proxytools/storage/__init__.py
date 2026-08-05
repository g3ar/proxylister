"""Persistent monitor state stored in the clone-local SQLite database."""

from proxytools.storage.sqlite import CheckObservation, StateRepository

__all__ = ["CheckObservation", "StateRepository"]
