"""Shared utilities for ETL pipelines."""

from contextlib import contextmanager

# Namespace key for advisory locks used to serialize account.move posting.
# Combined with journal_id as the second argument to pg_advisory_xact_lock.
_MOVE_POST_LOCK_NS = 0x4D565F50  # "MV_P" in hex, arbitrary non-colliding int


@contextmanager
def post_lock(cr, journal_id: int):
    """Acquire a transaction-level advisory lock to serialize move posting.

    Prevents deadlocks on the ``account_move_unique_name`` index when
    multiple workers concurrently post moves to the same journal.
    The lock is held until the current transaction ends (commit/rollback).

    Usage::

        move = ctx.env["account.move"].create(vals)
        with post_lock(ctx.env.cr, move.journal_id.id):
            move.action_post()
    """
    cr.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        [_MOVE_POST_LOCK_NS, journal_id],
    )
    yield
