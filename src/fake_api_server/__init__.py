"""In-memory fake API server for end-to-end tests."""

from fake_api_server.app import create_app

__all__ = ["create_app"]
__version__ = "1.0.0"
