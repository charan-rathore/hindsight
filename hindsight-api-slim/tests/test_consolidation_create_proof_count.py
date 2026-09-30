"""New observations count distinct surviving sources on every storage path."""

import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hindsight_api.engine.consolidation import consolidator as C


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["vchord", "native", "pg_textsearch", "store"])
@pytest.mark.parametrize("source_case", ["multiple", "duplicates", "deleted", "all_deleted", "single"])
async def test_create_observation_proof_count(backend, source_case):
    first, second, deleted = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source_ids = {
        "multiple": [first, second],
        "duplicates": [first, second, first],
        "deleted": [first, deleted, second, deleted],
        "all_deleted": [deleted],
        "single": [first],
    }[source_case]
    live = [mid for mid in source_ids if mid != deleted]
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"id": mid} for mid in {first, second}])
    conn.fetchrow = AsyncMock(return_value={"id": uuid.uuid4()})
    conn.executemany = AsyncMock()
    store = MagicMock()
    store.store_owned_for.return_value = backend == "store"
    store.get_memories = AsyncMock(return_value=[SimpleNamespace(unit_id=str(mid)) for mid in {first, second}])
    store.upsert_observation = AsyncMock()
    engine = SimpleNamespace(_backend=SimpleNamespace(ops=SimpleNamespace(uses_observation_sources_table=False)))
    config = SimpleNamespace(text_search_extension=backend, text_search_extension_native_language="english")

    with (
        patch.object(C, "get_memories", return_value=store),
        patch.object(C, "get_config", return_value=config),
        patch("hindsight_api.engine.schema._is_oracle", return_value=False),
    ):
        result = await C._apply_create_observation(
            conn, engine, "bank", source_ids, "A supported observation", "[0.1, 0.2]"
        )

    if not live:
        assert result == {"action": "skipped", "reason": "sources_deleted"}
        conn.fetchrow.assert_not_awaited()
        store.upsert_observation.assert_not_awaited()
        return

    assert result["action"] == "created"
    expected = len(set(live))
    if backend == "store":
        record = store.upsert_observation.await_args.kwargs["record"]
        assert record.proof_count == expected
        assert record.source_memory_ids == [str(mid) for mid in live]
        conn.fetchrow.assert_not_awaited()
    else:
        query, *args = conn.fetchrow.await_args.args
        # Resolve the proof_count SQL value, not just the Python argument.
        values = query.split("VALUES (", 1)[1].split(")", 1)[0].split(",")
        proof_value = values[5].strip()
        actual = args[int(proof_value[1:]) - 1] if proof_value.startswith("$") else int(proof_value)
        assert actual == expected
        assert args[4] == live
        assert max(map(int, re.findall(r"\$(\d+)", query))) == len(args)
        store.upsert_observation.assert_not_awaited()
