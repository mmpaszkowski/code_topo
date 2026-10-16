"""Enrich an existing index through clangd's LSP interface."""

import logging

from .client import ClangdClient
from .step import ClangdLspStep
from .transport import LspRequestError, LspSessionError

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = ["ClangdClient", "ClangdLspStep", "LspRequestError", "LspSessionError"]
