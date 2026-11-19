"""Local-only A-share research and historical simulation workbench."""

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    # Avoid opening the default user database when importing package modules.
    from .app import create_app as factory
    return factory(*args, **kwargs)

