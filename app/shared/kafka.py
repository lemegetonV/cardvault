"""Kafka helpers on aiokafka: topic creation, a producer and a consumer runner.

* Topics are created by the services at start-up (idempotent), 3 partitions each, so the
  stack needs no extra init container (rule 30, rule 66).
* The producer sends the correlation ID, event ID and event type as headers.
* ``Consumer`` processes one message at a time per partition and commits only after the
  handler succeeds, so delivery is at-least-once and every handler must be idempotent.
  Malformed events (schema or envelope version) go to a DLQ after 3 retries; other errors
  are retried with backoff and block the partition, preserving per-key order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from shared import correlation, events
from shared.faults import registry as faults
from shared.settings import BackoffSettings

log = logging.getLogger(__name__)


class KafkaPublishError(RuntimeError):
    pass


async def ensure_topics(bootstrap: str, topics=events.ALL_TOPICS) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = [
            NewTopic(name=t, num_partitions=events.PARTITIONS, replication_factor=1)
            for t in topics
            if t not in existing
        ]
        if missing:
            # Another service may win the race to create a topic; that is fine.
            with contextlib.suppress(TopicAlreadyExistsError):
                await admin.create_topics(missing)
            log.info("created topics", extra={"topics": [t.name for t in missing]})
    finally:
        await admin.close()


class Producer:
    def __init__(self, bootstrap: str) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=bootstrap, acks="all", enable_idempotence=True, linger_ms=5
        )
        self.started = False

    async def start(self) -> None:
        await self._producer.start()
        self.started = True

    async def stop(self) -> None:
        if self.started:
            await self._producer.stop()
            self.started = False

    async def ping(self) -> None:
        """Readiness check: the cluster answers a metadata request."""
        await asyncio.wait_for(self._producer.client.fetch_all_metadata(), timeout=3)

    async def send(
        self, topic: str, key: str, value: bytes, headers: dict[str, str] | None = None
    ) -> None:
        if faults.take("kafka_publish_failure", topic=topic):
            raise KafkaPublishError(f"injected publish failure on {topic}")
        hdrs = [(k, v.encode()) for k, v in (headers or {}).items()]
        await self._producer.send_and_wait(topic, value=value, key=key.encode(), headers=hdrs)

    async def send_envelope(self, topic: str, key: str, envelope: dict) -> None:
        headers = {
            "event_id": str(envelope["event_id"]),
            "event_type": str(envelope["event_type"]),
        }
        if envelope.get("correlation_id"):
            headers[correlation.HEADER] = envelope["correlation_id"]
        await self.send(topic, key, json.dumps(envelope, default=str).encode(), headers)


Handler = Callable[[events.Envelope], Awaitable[None]]


class Consumer:
    """Run ``handler`` for every event on ``topics`` within consumer group ``group_id``."""

    MALFORMED_RETRIES = 3

    def __init__(
        self,
        *,
        bootstrap: str,
        group_id: str,
        topics: list[str],
        handler: Handler,
        dlq_topic: str | None,
        producer: Producer | None,
        backoff: BackoffSettings,
    ) -> None:
        self.group_id = group_id
        self.topics = topics
        self.handler = handler
        self.dlq_topic = dlq_topic
        self.producer = producer
        self.backoff = backoff
        self._consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=bootstrap,
            group_id=group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        self._task: asyncio.Task | None = None
        self._paused = asyncio.Event()  # set while /test/reset holds the consumer
        self._idle = asyncio.Event()
        self._idle.set()

    async def start(self) -> None:
        await self._consumer.start()
        self._task = asyncio.create_task(self._run(), name=f"consumer-{self.group_id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._consumer.stop()

    async def hold(self) -> None:
        """Pause and wait for the in-flight message to finish (used by /test/reset)."""
        self._paused.set()
        await self._idle.wait()

    async def seek_to_end_and_commit(self) -> None:
        parts = self._consumer.assignment()
        if not parts:
            return
        ends = await self._consumer.end_offsets(list(parts))
        for tp, offset in ends.items():
            self._consumer.seek(tp, offset)
        await self._consumer.commit({tp: off for tp, off in ends.items()})

    def release(self) -> None:
        self._paused.clear()

    async def lag(self) -> int:
        parts = list(self._consumer.assignment())
        if not parts:
            return 0
        ends = await self._consumer.end_offsets(parts)
        total = 0
        for tp in parts:
            committed = await self._consumer.committed(tp)
            total += max(0, ends[tp] - (committed or 0))
        return total

    async def _run(self) -> None:
        while True:
            if self._paused.is_set() or faults.is_active("consumer_pause"):
                await asyncio.sleep(0.2)
                continue
            batch = await self._consumer.getmany(timeout_ms=500, max_records=50)
            for tp, messages in batch.items():
                for msg in messages:
                    self._idle.clear()
                    try:
                        await self._process(tp, msg)
                    finally:
                        self._idle.set()

    async def _process(self, tp: TopicPartition, msg) -> None:
        malformed_attempts = 0
        attempt = 0
        while True:
            try:
                envelope = events.parse(msg.value)
                token = correlation.set(envelope.correlation_id or correlation.new_id())
                try:
                    await self.handler(envelope)
                finally:
                    correlation.reset(token)
                break
            except events.MalformedEvent as exc:
                malformed_attempts += 1
                if malformed_attempts > self.MALFORMED_RETRIES:
                    await self._to_dlq(msg, str(exc))
                    break
                log.warning("malformed event, retrying", extra={"topic": msg.topic})
                await asyncio.sleep(self.backoff.delay(malformed_attempts))
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                log.exception("event handler failed, retrying", extra={"topic": msg.topic})
                await asyncio.sleep(self.backoff.delay(attempt))
        await self._consumer.commit({tp: msg.offset + 1})

    async def _to_dlq(self, msg, reason: str) -> None:
        log.error("event sent to DLQ", extra={"topic": msg.topic, "dlq": self.dlq_topic})
        if self.dlq_topic and self.producer:
            await self.producer.send(
                self.dlq_topic,
                (msg.key or b"").decode(errors="replace"),
                msg.value,
                {
                    "source_topic": msg.topic,
                    "source_partition": str(msg.partition),
                    "source_offset": str(msg.offset),
                    "error": reason[:500],
                },
            )
