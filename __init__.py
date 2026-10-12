"""Hermes plugin entry point for Ample."""

if __package__:
    from .hermes_ample.plugin import register
else:  # Direct source-tree import (pytest and local development).
    from hermes_ample.plugin import register

__all__ = ["register"]
