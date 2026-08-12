"""Connection management for the external-backend retain write-group.

The whole point of ``_streaming_batch_write_ext`` is that a store which owns its memory rows
in a separate system must NOT hold the data-plane Postgres connection across the (slow)
object-store write. These tests pin that contract:

* the memory writes (``insert_facts_batch`` + the entity re-posting) happen while NO connection
  is checked out;
* the connection is taken only for the short witness transaction (document/chunk metadata,
  entity-registry reassert, outbox, ``write_txn_witness``);
* ``decide_txn(commit=True)`` publishes the group after the connection is released;
* a later-batch takeover discards the staged write via ``decide_txn(commit=False)`` — and the
  staged write was still connection-free;
* the Postgres link writers (temporal/semantic/causal) are never invoked for an ext org.
"""

from types import SimpleNamespace

import pytest

import hindsight_api.engine.retain.orchestrator as orch


class _ConnTracker:
    """Flips ``open`` while a connection is checked out via acquire_with_retry."""

    def __init__(self):
        self.open = False
        self.store_writes_saw_open = []  # records `open` at each store-write call

    def acquire(self):
        tracker = self

        class _CM:
            async def __aenter__(self_inner):
                tracker.open = True
                return SimpleNamespace(name="conn", transaction=_txn)
            async def __aexit__(self_inner, *a):
                tracker.open = False
                return False

        return _CM()


def _txn():
    class _T:
        async def __aenter__(self):
            return None
        async def __aexit__(self, *a):
            return False
    return _T()


def _make_common(monkeypatch, tracker, *, calls):
    """Patch the module-level collaborators the helper reaches for."""
    monkeypatch.setattr(orch, "acquire_with_retry", lambda pool: tracker.acquire())

    async def _insert_facts_batch(conn, bank_id, processed, ops=None, txn=None):
        calls.append(("insert_facts", conn, tracker.open))
        tracker.store_writes_saw_open.append(tracker.open)
        assert conn is None, "ext store write must not receive a connection"
        return ["u1"]

    async def _store_chunks_batch(conn, bank_id, doc_id, meta, ops=None, store_document_text=True):
        calls.append(("store_chunks", tracker.open))
        return {}

    async def _handle_doc_tracking(conn, *a, **k):
        calls.append(("handle_doc_tracking", tracker.open))

    monkeypatch.setattr(orch.fact_storage, "insert_facts_batch", _insert_facts_batch)
    monkeypatch.setattr(orch.chunk_storage, "store_chunks_batch", _store_chunks_batch)
    monkeypatch.setattr(orch.fact_storage, "handle_document_tracking", _handle_doc_tracking)
    monkeypatch.setattr(orch, "_map_results_to_contents", lambda contents, pf, uids: [list(uids)])
    monkeypatch.setattr(
        orch, "_remap_phase1_results", lambda rids, e2u, u2e, sem, uids: ([("u1", 0, None)], {}, [])
    )
    # Any PG link writer being called for an ext org is a bug — make it explode.
    for name in ("create_temporal_links_batch", "create_semantic_links_batch", "create_causal_links_batch"):
        if hasattr(orch.link_creation, name):
            def _boom(*a, _n=name, **k):
                raise AssertionError(f"ext path must not call link_creation.{_n}")
            monkeypatch.setattr(orch.link_creation, name, _boom)


class _Provider:
    def __init__(self):
        self.decisions = []
        self.witnesses = []

    async def write_txn_witness(self, txn, *, conn, fq_table):
        self.witnesses.append((txn, conn))

    async def decide_txn(self, txn, *, commit):
        self.decisions.append(commit)


class _EntityResolver:
    def __init__(self, tracker):
        self._t = tracker
        self.postings = []
        self.reasserts = []

    async def record_unit_entity_postings(self, pairs, bank_id=None):
        # THE contract: the store re-posting runs with no connection held.
        assert self._t.open is False, "entity posting must run connection-free"
        self.postings.append(pairs)

    async def reassert_entities_batch(self, bank_id, resolved, conn):
        assert conn is not None
        self.reasserts.append(bank_id)


def _kwargs(tracker, provider, er, *, doc_tracking_done, existing_hash, new_hash, outbox=None, is_last=False):
    async def _lock(conn, table, doc_id, bank_id):
        return existing_hash

    pool = SimpleNamespace(ops=SimpleNamespace(lock_document_for_write=_lock))
    phase1 = SimpleNamespace(
        entities=SimpleNamespace(
            resolved_entities=[SimpleNamespace(entity_id="e1")],
            entity_to_unit=[(0, 0, None)],
            unit_to_entity_ids={},
        )
    )
    return dict(
        provider=provider,
        ext_txn=SimpleNamespace(txn_id="t1"),
        pool=pool,
        bank_id="bank1",
        fq_table=lambda t: t,
        entity_resolver=er,
        phase1=phase1,
        batch_contents=[{"content": "c"}],
        batch_extracted=[SimpleNamespace(chunk_index=None)],
        batch_processed=[SimpleNamespace(document_id=None, chunk_id=None)],
        batch_chunk_meta=[],
        effective_doc_id="doc1",
        config=SimpleNamespace(store_document_text=True),
        log_buffer=[],
        is_recovery=False,
        is_first_batch=True,
        is_last=is_last,
        doc_tracking_done=doc_tracking_done,
        pipeline_aborted=[False],
        append_base_hash=None,
        new_content_hash=new_hash,
        combined_content="body",
        retain_params=None,
        merged_tags=[],
        outbox_callback=outbox,
        assert_append_base_unchanged=lambda h: None,
        p2_start=0.0,
    )


async def test_store_writes_are_connection_free_and_witness_is_in_txn(monkeypatch):
    tracker = _ConnTracker()
    calls = []
    _make_common(monkeypatch, tracker, calls=calls)
    provider, er = _Provider(), _EntityResolver(tracker)

    aborted, result_ids = await orch._streaming_batch_write_ext(
        **_kwargs(tracker, provider, er, doc_tracking_done=[False], existing_hash="__pending__", new_hash="h")
    )

    assert aborted is False
    assert result_ids == [["u1"]]
    # The fact write happened with no connection held.
    assert tracker.store_writes_saw_open == [False]
    # Entity re-posting happened (also connection-free — asserted inside the fake).
    assert er.postings == [[("u1", "e1", None)]]
    # Witness written with a real connection, exactly once; commit published after release.
    assert len(provider.witnesses) == 1 and provider.witnesses[0][1] is not None
    assert provider.decisions == [True]
    assert tracker.open is False  # connection released at the end
    # Entity registry reassert ran inside the txn.
    assert er.reasserts == ["bank1"]


async def test_later_batch_takeover_aborts_and_discards_staged_write(monkeypatch):
    tracker = _ConnTracker()
    calls = []
    _make_common(monkeypatch, tracker, calls=calls)
    provider, er = _Provider(), _EntityResolver(tracker)

    # Later batch (doc_tracking already done) whose document was taken over: existing hash
    # differs from ours → abort.
    aborted, _ = await orch._streaming_batch_write_ext(
        **_kwargs(tracker, provider, er, doc_tracking_done=[True], existing_hash="OTHER", new_hash="OURS")
    )

    assert aborted is True
    # Staged store write still happened connection-free before the takeover was detected.
    assert tracker.store_writes_saw_open == [False]
    # The group was explicitly aborted, not committed.
    assert provider.decisions == [False]


async def test_outbox_row_rides_the_connection(monkeypatch):
    tracker = _ConnTracker()
    calls = []
    _make_common(monkeypatch, tracker, calls=calls)
    provider, er = _Provider(), _EntityResolver(tracker)
    seen = {}

    async def _outbox(conn):
        seen["open"] = tracker.open
        seen["conn"] = conn

    aborted, _ = await orch._streaming_batch_write_ext(
        **_kwargs(
            tracker, provider, er, doc_tracking_done=[False],
            existing_hash="__pending__", new_hash="h", outbox=_outbox, is_last=True,
        )
    )
    assert aborted is False
    assert seen["open"] is True and seen["conn"] is not None  # outbox wrote inside the txn
    assert provider.decisions == [True]
