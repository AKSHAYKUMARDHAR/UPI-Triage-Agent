"""Bank statement import: any supported netbanking export -> pipeline rows."""
from ingest.statement import StatementError, parse_statement

__all__ = ["StatementError", "parse_statement"]
