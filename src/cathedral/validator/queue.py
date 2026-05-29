"""Claim-status aggregation against sqlite.

The legacy claim queue (insert/claim/verify/reject) was removed with the
validator card surface. The only surviving read is ``counts_by_status``,
which the stall watchdog mirrors into ``/health`` for operator visibility.
The ``claims`` table itself is left in the schema (db.py) so historical
rows and the health counters keep working; it is no longer written.
"""

from __future__ import annotations

import aiosqlite


async def counts_by_status(conn: aiosqlite.Connection) -> dict[str, int]:
    cur = await conn.execute("SELECT status, COUNT(*) FROM claims GROUP BY status")
    rows = await cur.fetchall()
    return {str(r[0]): int(r[1]) for r in rows}
