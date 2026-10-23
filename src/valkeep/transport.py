"""JSON 파싱 이전에 줄 크기를 제한하는 POSIX stdio 전송."""

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from valkeep.errors import Code, Rejected


@asynccontextmanager
async def message_streams(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    read_limit: int,
    write_limit: int,
) -> AsyncIterator[tuple]:
    incoming_tx, incoming_rx = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    outgoing_tx, outgoing_rx = anyio.create_memory_object_stream[SessionMessage](1)

    async def receive():
        async with incoming_tx:
            try:
                while raw := await reader.readline():
                    if len(raw) > read_limit or not raw.endswith(b"\n"):
                        raise Rejected(Code.INVALID_ARGUMENT)
                    message = JSONRPCMessage.model_validate_json(raw)
                    await incoming_tx.send(SessionMessage(message))
            except Exception:
                # 파싱 오류에 들어 있는 원본 프레임은 전달하지 않는다.
                with contextlib.suppress(anyio.BrokenResourceError):
                    await incoming_tx.send(Rejected(Code.UPSTREAM_UNAVAILABLE))

    async def send():
        async with outgoing_rx:
            async for item in outgoing_rx:
                raw = (
                    item.message.model_dump_json(by_alias=True, exclude_none=True).encode() + b"\n"
                )
                if len(raw) > write_limit:
                    raise Rejected(Code.RESPONSE_TOO_LARGE)
                writer.write(raw)
                await writer.drain()

    reader_task = asyncio.create_task(receive())
    writer_task = asyncio.create_task(send())

    # 쓰기 실패도 입력 종료로 전달하여 SDK의 대기가 남지 않게 한다.
    def writer_done(task):
        if not task.cancelled() and task.exception() is not None:
            reader_task.cancel()

    writer_task.add_done_callback(writer_done)
    try:
        yield incoming_rx, outgoing_tx
    finally:
        reader_task.cancel()
        writer_task.cancel()
        await asyncio.gather(reader_task, writer_task, return_exceptions=True)
        await incoming_rx.aclose()
        await outgoing_tx.aclose()


@asynccontextmanager
async def public_stdio(read_limit: int) -> AsyncIterator[tuple]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=read_limit)
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    write_transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(write_transport, protocol, None, loop)
    try:
        async with message_streams(reader, writer, read_limit, 131072) as streams:
            yield streams
    finally:
        read_transport.close()
        writer.close()
