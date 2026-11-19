"""Offline regressions for latest-only startup, live events and source polling."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from telethon.tl.types import PeerChannel
from telethon.utils import get_peer_id

from app import db, listener
from app.config import Config


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from app.links import extract_asin

    monkeypatch.setattr(
        listener,
        "fetch_product_title",
        Mock(side_effect=lambda url: f"Amazon product {extract_asin(url)}"),
        raising=False,
    )
    monkeypatch.setattr(listener, "notify_error", AsyncMock())
    monkeypatch.setattr(
        listener,
        "TelegramClient",
        Mock(side_effect=AssertionError("a real Telegram client must not be created")),
    )


@pytest.fixture()
def sources():
    return [
        listener.Source(
            label=f"@source_{number}",
            entity=PeerChannel(number),
            names=(f"Source {number}",),
            usernames=(f"source_{number}",),
        )
        for number in range(1, 4)
    ]


@pytest.fixture()
def config(tmp_path, sources):
    return Config(
        tg_api_id=123,
        tg_api_hash="offline-test-hash",
        tg_session_path=tmp_path / "session",
        source_channels=[source.label for source in sources],
        db_path=tmp_path / "offers.db",
        media_dir=tmp_path / "media",
        catchup_limit=50,
        catchup_delay_seconds=0,
        require_amazon_link=True,
        amazon_associate_tag="ours-21",
        amazon_tag_confirmed=True,
        amazon_marketplace="www.amazon.eg",
        max_per_source_per_hour=100,
    )


@pytest.fixture()
def conn(tmp_path):
    connection = db.init_db(tmp_path / "offers.db")
    yield connection
    connection.close()


def message(msg_id, text):
    return SimpleNamespace(
        id=msg_id,
        message=text,
        photo=None,
        get_entities_text=lambda: [],
    )


def amazon_message(msg_id=7, *, title="Wireless Headphones", asin="B000000001", price=49):
    return message(
        msg_id,
        f"{title}\nPrice: ${price}\nhttps://www.amazon.eg/dp/{asin}",
    )


def history_client(histories):
    # Telethon returns newest first and applies the requested cap server-side.
    async def get_messages(entity, *, limit):
        return histories[entity.channel_id][:limit]

    return SimpleNamespace(get_messages=AsyncMock(side_effect=get_messages))


@pytest.fixture()
def histories(sources):
    titles = ("Wireless Headphones", "Countertop Air Fryer", "Portable Solid State Drive")
    return {
        source.entity.channel_id: [
            amazon_message(20, title=title, asin=f"B{number:09d}", price=number * 50),
            amazon_message(19, title=f"Older {title}", asin=f"B{number + 10:09d}"),
        ]
        for number, (source, title) in enumerate(zip(sources, titles), start=1)
    }


def event_for(client, source, post):
    return SimpleNamespace(
        client=client,
        chat_id=get_peer_id(source.entity),
        message=post,
        # Resolved source metadata must win over event-side aliases.
        chat=SimpleNamespace(username="unexpected_alias", title="Unexpected title"),
    )


def test_monitoring_defaults_are_latest_only_with_thirty_second_polling():
    config = Config()
    assert config.listener_latest_only is True
    assert config.source_poll_interval_seconds == 30


@pytest.mark.parametrize("limit", [None, 1, 500])
def test_latest_only_fetches_one_message_from_every_source(
    config, sources, histories, monkeypatch, limit
):
    client = history_client(histories)
    process = AsyncMock(return_value=1)
    monkeypatch.setattr(listener, "process_message", process)
    connection = object()

    stored = asyncio.run(listener.catch_up(client, connection, config, sources, limit=limit))

    assert stored == len(sources)
    assert client.get_messages.await_args_list == [
        call(source.entity, limit=1) for source in sources
    ]
    assert process.await_count == len(sources)
    for invocation, source in zip(process.await_args_list, sources):
        assert invocation.args == (client, histories[source.entity.channel_id][0], source.label)
        assert invocation.kwargs["conn"] is connection
        assert invocation.kwargs["config"] is config
        assert invocation.kwargs["source_names"] == source.names
        assert invocation.kwargs["source_usernames"] == source.usernames


@pytest.mark.parametrize("text", ["Channel announcement", "Other shop\nhttps://shop.example/p/1"])
def test_newest_non_amazon_post_does_not_trigger_backwards_search(
    conn, config, sources, text
):
    source = sources[0]
    client = history_client({
        source.entity.channel_id: [message(20, text), amazon_message(19)],
    })

    assert asyncio.run(listener.catch_up(client, conn, config, [source])) == 0

    client.get_messages.assert_awaited_once_with(source.entity, limit=1)
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


def test_source_fetch_failure_does_not_prevent_other_sources(
    config, sources, histories, monkeypatch
):
    config.catchup_limit = 1
    client = history_client(histories)
    read_history = client.get_messages.side_effect

    async def fetch(entity, *, limit):
        if entity == sources[0].entity:
            raise RuntimeError("source unavailable")
        return await read_history(entity, limit=limit)

    client.get_messages.side_effect = fetch
    process = AsyncMock(return_value=1)
    monkeypatch.setattr(listener, "process_message", process)

    assert asyncio.run(listener.catch_up(client, object(), config, sources)) == 2
    assert client.get_messages.await_args_list == [
        call(source.entity, limit=1) for source in sources
    ]
    assert [invocation.args[2] for invocation in process.await_args_list] == [
        source.label for source in sources[1:]
    ]


def test_bad_latest_message_does_not_prevent_other_sources(
    config, sources, histories, monkeypatch
):
    config.catchup_limit = 1
    client = history_client(histories)
    process = AsyncMock(side_effect=[ValueError("bad post"), 1, 1])
    monkeypatch.setattr(listener, "process_message", process)

    assert asyncio.run(listener.catch_up(client, object(), config, sources)) == 2
    assert [invocation.args[2] for invocation in process.await_args_list] == [
        source.label for source in sources
    ]
    listener.notify_error.assert_awaited_once()


@pytest.mark.parametrize("configured_limit, limit", [(0, None), (50, 0), (0, 0)])
def test_zero_cap_does_not_read_any_source(
    config, sources, monkeypatch, configured_limit, limit
):
    config.catchup_limit = configured_limit
    client = SimpleNamespace(get_messages=AsyncMock())
    process = AsyncMock()
    monkeypatch.setattr(listener, "process_message", process)

    assert asyncio.run(listener.catch_up(client, object(), config, sources, limit=limit)) == 0
    client.get_messages.assert_not_called()
    process.assert_not_called()


@pytest.mark.parametrize("limit, expected_cap", [(None, 2), (1, 1), (10, 10)])
def test_legacy_backfill_honors_configured_and_explicit_limits(
    config, sources, histories, monkeypatch, limit, expected_cap
):
    config.listener_latest_only = False
    config.catchup_limit = 2
    client = history_client(histories)
    process = AsyncMock(return_value=1)
    monkeypatch.setattr(listener, "process_message", process)

    stored = asyncio.run(listener.catch_up(client, object(), config, sources, limit=limit))

    expected = [
        (post.id, source.label)
        for source in sources
        for post in reversed(histories[source.entity.channel_id][:expected_cap])
    ]
    assert client.get_messages.await_args_list == [
        call(source.entity, limit=expected_cap) for source in sources
    ]
    assert [(item.args[1].id, item.args[2]) for item in process.await_args_list] == expected
    assert stored == len(expected)


@pytest.mark.parametrize("use_lock", [False, True])
def test_live_messages_route_each_source_with_its_own_metadata(
    config, sources, histories, monkeypatch, use_lock
):
    process = AsyncMock(return_value=1)
    monkeypatch.setattr(listener, "process_message", process)
    client, connection = object(), object()
    by_chat = {get_peer_id(source.entity): source for source in sources}

    async def check():
        lock = asyncio.Lock()
        for source in reversed(sources):
            post = histories[source.entity.channel_id][0]
            await listener.on_new_message(
                event_for(client, source, post),
                conn=connection,
                config=config,
                sources=by_chat,
                **({"lock": lock} if use_lock else {}),
            )
        for invocation, source in zip(process.await_args_list, reversed(sources)):
            assert invocation.args == (client, histories[source.entity.channel_id][0], source.label)
            assert invocation.kwargs["conn"] is connection
            assert invocation.kwargs["config"] is config
            assert invocation.kwargs["source_names"] == source.names
            assert invocation.kwargs["source_usernames"] == source.usernames
            if use_lock:
                assert invocation.kwargs["lock"] is lock

    asyncio.run(check())
    assert process.await_count == len(sources)


@pytest.mark.parametrize("use_lock", [False, True])
def test_startup_poll_and_live_keep_source_and_content_deduplication(
    conn, config, sources, use_lock
):
    sources = sources[:2]
    first = amazon_message()
    second = amazon_message(7, title="Countertop Air Fryer", asin="B000000002", price=99)
    client = history_client({
        sources[0].entity.channel_id: [first],
        sources[1].entity.channel_id: [second],
    })
    by_chat = {get_peer_id(source.entity): source for source in sources}

    async def check():
        kwargs = {"lock": asyncio.Lock()} if use_lock else {}
        assert await listener.catch_up(client, conn, config, sources, **kwargs) == 2
        assert await listener.catch_up(client, conn, config, sources, **kwargs) == 0
        for source, post in zip(sources, (first, second)):
            await listener.on_new_message(
                event_for(client, source, post), conn=conn, config=config,
                sources=by_chat, **kwargs,
            )
        # Editing a seen message cannot bypass (source, message-id) dedupe.
        edited = amazon_message(7, title="Unrelated Camera", asin="B000000003", price=499)
        # Both a same-source repost and a cross-source copy keep exact-hash dedupe.
        for source, post in (
            (sources[0], edited),
            (sources[0], amazon_message(8)),
            (sources[1], amazon_message(9)),
        ):
            await listener.on_new_message(
                event_for(client, source, post), conn=conn, config=config,
                sources=by_chat, **kwargs,
            )

    asyncio.run(check())
    rows = conn.execute(
        "SELECT source_channel, source_msg_id, status FROM offers ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (source.label, 7, "pending") for source in sources
    ]


@pytest.mark.parametrize("entrypoint", ["catch_up", "on_new_message", "process_message"])
def test_shared_lock_blocks_processing_before_source_dedupe(
    conn, config, sources, monkeypatch, entrypoint
):
    source = sources[0]
    post = amazon_message()
    client = history_client({source.entity.channel_id: [post]})
    lookup = Mock(wraps=db.offer_exists_by_source)
    monkeypatch.setattr(listener.db, "offer_exists_by_source", lookup)

    async def check():
        lock = asyncio.Lock()

        async def process():
            if entrypoint == "catch_up":
                return await listener.catch_up(client, conn, config, [source], lock=lock)
            if entrypoint == "on_new_message":
                return await listener.on_new_message(
                    event_for(client, source, post), conn=conn, config=config,
                    sources={get_peer_id(source.entity): source}, lock=lock,
                )
            return await listener.process_message(
                client, post, source.label, conn=conn, config=config, lock=lock,
            )

        async with lock:
            task = asyncio.create_task(process())
            await asyncio.sleep(0)
            blocked = not task.done() and lookup.call_count == 0
        await asyncio.wait_for(task, timeout=3)
        assert blocked, "processing must acquire the lock before checking source dedupe"

    asyncio.run(check())
    assert lookup.call_count == 1
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1


@pytest.fixture()
def runtime(config, sources, monkeypatch):
    connection = Mock()
    client = SimpleNamespace(
        connect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        start=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(id=42, username="offline")),
        add_event_handler=Mock(),
        run_until_disconnected=AsyncMock(),
        disconnect=AsyncMock(),
    )
    monkeypatch.setattr(listener.db, "init_db", Mock(return_value=connection))
    monkeypatch.setattr(listener, "TelegramClient", Mock(return_value=client))
    monkeypatch.setattr(listener, "resolve_sources", AsyncMock(return_value=sources))
    return SimpleNamespace(client=client, conn=connection)


def test_run_registers_all_sources_before_startup_and_shares_the_lock(
    config, sources, runtime, monkeypatch
):
    process = AsyncMock(return_value=1)
    monkeypatch.setattr(listener, "process_message", process)

    async def startup(client, connection, cfg, resolved, *, limit=None, lock=None):
        runtime.client.add_event_handler.assert_called_once()
        handler, watcher = runtime.client.add_event_handler.call_args.args
        assert watcher.chats == [source.entity for source in sources]
        assert client is runtime.client
        assert connection is runtime.conn
        assert cfg is config
        assert resolved == sources
        assert limit == 500
        assert isinstance(lock, asyncio.Lock)
        # A live event can arrive while startup catch-up is still in progress.
        for source in sources:
            await handler(event_for(client, source, amazon_message()))
        assert [item.args[2] for item in process.await_args_list] == [
            source.label for source in sources
        ]
        assert all(item.kwargs["lock"] is lock for item in process.await_args_list)
        return 0

    catch_up = AsyncMock(side_effect=startup)
    monkeypatch.setattr(listener, "catch_up", catch_up)
    asyncio.run(listener.run(config, limit=500, non_interactive=True))
    catch_up.assert_awaited_once()
    runtime.client.disconnect.assert_awaited_once()
    runtime.conn.close.assert_called_once()


@pytest.mark.parametrize(
    "startup, shutdown",
    [(True, "normal"), (False, "normal"), (True, "error"), (True, "cancel")],
)
def test_run_polls_every_source_and_stops_poller_before_resource_cleanup(
    config, sources, histories, runtime, monkeypatch, startup, shutdown
):
    config.source_poll_interval_seconds = 7
    if not startup:
        # Disabling startup/backfill must not disable latest-only monitoring.
        config.listener_latest_only = False
    runtime.client.get_messages = history_client(histories).get_messages
    process = AsyncMock(return_value=1)
    monkeypatch.setattr(listener, "process_message", process)
    catch_up = AsyncMock(wraps=listener.catch_up)
    monkeypatch.setattr(listener, "catch_up", catch_up)
    timeline = []
    poll_tasks = []
    delays = []
    runtime.client.disconnect.side_effect = lambda: timeline.append("disconnect")
    runtime.conn.close.side_effect = lambda: timeline.append("close")

    async def check():
        ticks = asyncio.Queue()
        sleeping = asyncio.Event()

        async def poll_sleep(delay):
            delays.append(delay)
            poll_tasks.append(asyncio.current_task())
            sleeping.set()
            try:
                await ticks.get()
            except asyncio.CancelledError:
                timeline.append("poll-cancelled")
                raise

        clock = SimpleNamespace(**vars(asyncio))
        clock.sleep = poll_sleep
        monkeypatch.setattr(listener, "asyncio", clock)

        async def disconnected():
            await asyncio.wait_for(sleeping.wait(), timeout=2)
            baseline = runtime.client.get_messages.await_count
            for _ in range(2):
                sleeping.clear()
                ticks.put_nowait(None)
                await asyncio.wait_for(sleeping.wait(), timeout=2)
            assert runtime.client.get_messages.await_count == baseline + 2 * len(sources)
            if shutdown == "error":
                raise RuntimeError("offline disconnect")
            if shutdown == "cancel":
                asyncio.current_task().cancel()
                await asyncio.Event().wait()

        runtime.client.run_until_disconnected.side_effect = disconnected
        operation = listener.run(config, catchup=startup, limit=500, non_interactive=True)
        if shutdown == "error":
            with pytest.raises(RuntimeError, match="offline disconnect"):
                await asyncio.wait_for(operation, timeout=6)
        elif shutdown == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, timeout=6)
        else:
            await asyncio.wait_for(operation, timeout=6)
        assert poll_tasks and all(task.done() for task in poll_tasks)
        assert timeline.index("poll-cancelled") < timeline.index("disconnect")
        assert timeline.index("poll-cancelled") < timeline.index("close")

    asyncio.run(check())
    assert delays and all(delay == 7 for delay in delays)
    runtime.client.add_event_handler.assert_called_once()
    _, watcher = runtime.client.add_event_handler.call_args.args
    assert watcher.chats == [source.entity for source in sources]
    reads = runtime.client.get_messages.await_args_list
    sweep = [call(source.entity, limit=1) for source in sources]
    assert len(reads) >= 2 * len(sources)
    assert len(reads) % len(sources) == 0
    for offset in range(0, len(reads), len(sources)):
        assert reads[offset:offset + len(sources)] == sweep
    invocations = catch_up.await_args_list
    if startup:
        assert invocations[0].kwargs["limit"] == 500
        polls = invocations[1:]
    else:
        polls = invocations
    assert len(polls) >= 2
    assert all(item.args == (runtime.client, runtime.conn, config, sources) for item in polls)
    assert all(item.kwargs["limit"] == 1 for item in polls)
    lock = invocations[0].kwargs["lock"]
    assert isinstance(lock, asyncio.Lock)
    assert all(item.kwargs["lock"] is lock for item in invocations)
    assert all(item.kwargs["lock"] is lock for item in process.await_args_list)
    runtime.client.disconnect.assert_awaited_once()
    runtime.conn.close.assert_called_once()


@pytest.mark.parametrize("catchup", [False, True])
def test_once_never_registers_live_watchers_or_starts_a_poller(
    config, sources, runtime, monkeypatch, catchup
):
    catch_up = AsyncMock(return_value=0)
    monkeypatch.setattr(listener, "catch_up", catch_up)
    clock = SimpleNamespace(**vars(asyncio))
    clock.create_task = Mock(wraps=asyncio.create_task)
    monkeypatch.setattr(listener, "asyncio", clock)

    asyncio.run(listener.run(config, catchup=catchup, once=True, limit=500, non_interactive=True))

    assert catch_up.await_count == int(catchup)
    if catchup:
        assert catch_up.await_args.args == (runtime.client, runtime.conn, config, sources)
        assert catch_up.await_args.kwargs["limit"] == 500
    runtime.client.add_event_handler.assert_not_called()
    runtime.client.run_until_disconnected.assert_not_called()
    clock.create_task.assert_not_called()
    runtime.client.disconnect.assert_awaited_once()
    runtime.conn.close.assert_called_once()
