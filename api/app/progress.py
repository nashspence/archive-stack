from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from .config import REDIS_URL, STREAM_MAXLEN

def redis_client() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


async def publish_progress(stream_name: str, payload: dict) -> None:
    redis = redis_client()
    try:
        await redis.xadd(stream_name, payload, maxlen=STREAM_MAXLEN, approximate=True)
    finally:
        await redis.aclose()


def progress_stream(stream_name: str) -> StreamingResponse:
    async def event_iter() -> AsyncIterator[bytes]:
        redis = redis_client()
        try:
            last_id = "0-0"
            while True:
                entries = await redis.xread({stream_name: last_id}, block=15000, count=50)
                if not entries:
                    yield b": keepalive\n\n"
                    continue
                for _stream, messages in entries:
                    for message_id, fields in messages:
                        last_id = message_id
                        payload = json.dumps({"id": message_id, **fields}, separators=(",", ":"))
                        yield f"data: {payload}\n\n".encode()
        finally:
            await redis.aclose()

    return StreamingResponse(event_iter(), media_type="text/event-stream")


def upload_stream_name(upload_id: str) -> str:
    return f"progress:uploads:{upload_id}"


def collection_stream_name(collection_id: str) -> str:
    return f"progress:collections:{collection_id}"


def activation_session_stream_name(session_id: str) -> str:
    return f"progress:activation-sessions:{session_id}"


def download_stream_name(session_id: str) -> str:
    return f"progress:downloads:{session_id}"
