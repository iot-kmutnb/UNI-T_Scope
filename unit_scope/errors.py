"""Exceptions raised by the unit_scope library."""


class ScopeError(Exception):
    """Base class for all unit_scope errors."""


class ScopeConnectionError(ScopeError):
    """The scope could not be found / opened, or the connection was lost."""


class ScopeCommandError(ScopeError):
    """A command failed or the scope's reply could not be understood."""
