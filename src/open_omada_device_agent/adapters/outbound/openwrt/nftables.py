"""Helpers for loading agent-owned nftables tables."""
from __future__ import annotations


def replace_table_script(family: str, name: str, table: str) -> str:
    """One ``nft -f -`` script that swaps ``table`` in for the old table.

    nft applies a file as a single transaction, so the old rules stay until
    the new table loads, and a failed load leaves them in place. The empty
    ``table`` line creates the table when it does not exist yet, so the
    ``delete`` cannot fail.
    """
    return f"table {family} {name} {{}}\ndelete table {family} {name}\n{table}"
