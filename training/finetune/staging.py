"""Bounded, sampler-free CPU collation and optional CUDA input transfers."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import time

import torch

from training.finetune import collate as co


@dataclass(eq=False)
class _Ticket:
    reservation: int
    future: Future | None = None
    host_batch: dict | None = None
    acquired: bool = False
    start_event: object = None
    end_event: object = None
    timing: dict = field(default_factory=lambda: {"collation_seconds": 0., "h2d_seconds": 0., "input_wait_seconds": 0.})


class BatchStager:
    """Queue at most a current batch and one lookahead batch for preparation.

    ``max_host_bytes`` bounds collated tensor storage, including the transient
    pageable copy during pinning. Dataset-owned example storage is not copied.
    Queue or byte pressure raises MemoryError so callers can use their normal
    synchronous path. All public operations belong to the training thread.
    Resolve acquired tickets after the optimizer boundary synchronization.
    """

    def __init__(self, backend, pad_id, max_host_bytes, enabled=False, max_queue_depth=2):
        if max_host_bytes < 0:
            raise ValueError("max_host_bytes must be nonnegative")
        if max_queue_depth not in (1, 2):
            raise ValueError("max_queue_depth must be one or two batches")
        self.backend, self.pad_id = backend, pad_id
        self.max_host_bytes = int(max_host_bytes)
        self.host_budget_cap = int(max_host_bytes)
        self.allow_prefetch = True
        self.max_queue_depth = max_queue_depth
        self._enabled, self._cuda = False, False
        self._worker = None
        self._stream = None
        self._tickets = set()
        self.pending_bytes = 0
        self._closed = False
        self.enabled = enabled

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        value = bool(value) and self.max_host_bytes > 0 and self.allow_prefetch
        if value == self._enabled:
            return
        if self._closed:
            raise RuntimeError("batch stager is closed")
        if self._tickets:
            raise RuntimeError("cannot change staging with pending tickets")
        cuda = value and self.backend.name == "cuda"
        if cuda and self._stream is None:
            self._stream = torch.cuda.Stream(device=self.backend.device)
        if value and self._worker is None:
            self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch-stager")
        self._enabled, self._cuda = value, cuda

    def _prepare(self, examples):
        started = time.monotonic()
        batch = co.collate([e.encoded for e in examples], self.pad_id,
                           label_states=[e.label_state for e in examples], weights=[e.weight for e in examples])
        pinned = False
        if self._cuda:
            try:
                # Keep the original dict until all tensors have pinned copies.
                batch = {k: v.pin_memory() for k, v in batch.items()}
                pinned = True
            except RuntimeError:
                # Pinning is optional, for example under a low locked-memory
                # limit. Pageable transfer uses the synchronous path below.
                pass
        return batch, time.monotonic() - started, pinned

    def submit(self, examples):
        if self._closed:
            raise RuntimeError("batch stager is closed")
        if not examples:
            raise ValueError("cannot stage an empty batch")
        length = max(len(e.encoded.input_ids) for e in examples)
        # Four int64 sequence tensors, two int64 vectors and one float32 vector.
        reservation = len(examples) * (length * 32 + 20) * (2 if self._cuda else 1)
        queued = sum(not ticket.acquired for ticket in self._tickets)
        if queued >= self.max_queue_depth or reservation + self.pending_bytes > self.max_host_bytes:
            raise MemoryError("batch staging host memory or queue budget exhausted")
        ticket = _Ticket(reservation)
        self._tickets.add(ticket)
        self.pending_bytes += reservation
        try:
            if self.enabled:
                ticket.future = self._worker.submit(self._prepare, tuple(examples))
            else:
                ticket.future = Future()
                try:
                    ticket.future.set_result(self._prepare(examples))
                except Exception as error:
                    ticket.future.set_exception(error)
        except BaseException:
            self._release(ticket)
            raise
        return ticket

    def _check(self, ticket):
        if ticket not in self._tickets:
            raise ValueError("unknown or already resolved staging ticket")

    def acquire(self, ticket):
        self._check(ticket)
        if ticket.acquired:
            raise ValueError("staging ticket was already acquired")
        started = time.monotonic()
        try:
            ticket.host_batch, collate_seconds, pinned = ticket.future.result()
            ticket.future = None
            ticket.timing.update(collation_seconds=collate_seconds, input_wait_seconds=time.monotonic() - started,
                                 pinned=pinned)
            # The loss can derive supervised positions on CPU without a GPU
            # nonzero/shape synchronization. Consumers pop this transient tuple
            # before retaining or serializing the timing report.
            ticket.timing["loss_metadata"] = (ticket.host_batch["labels"], ticket.host_batch["field_ids"])
            ticket.acquired = True
            if self._cuda and pinned:
                ticket.start_event = torch.cuda.Event(enable_timing=True)
                ticket.end_event = torch.cuda.Event(enable_timing=True)
                with torch.cuda.stream(self._stream):
                    ticket.start_event.record(self._stream)
                    device_batch = {k: v.to(self.backend.device, non_blocking=True) for k, v in ticket.host_batch.items()}
                    ticket.end_event.record(self._stream)
                current = torch.cuda.current_stream(self.backend.device)
                current.wait_stream(self._stream)
                for tensor in device_batch.values():
                    tensor.record_stream(current)
            else:
                transfer_start = time.monotonic()
                device_batch = {k: v.to(self.backend.device) for k, v in ticket.host_batch.items()}
                if self.backend.name == "cuda":
                    ticket.timing["h2d_seconds"] = time.monotonic() - transfer_start
            return device_batch, ticket.timing
        except BaseException:
            # A copy may have partially enqueued before OOM; retain its sources
            # until the copy stream has finished using them.
            if self._stream is not None:
                self._stream.synchronize()
            self._release(ticket)
            raise

    def _release(self, ticket):
        ticket.timing.pop("loss_metadata", None)
        ticket.host_batch = None
        ticket.future = None
        if ticket in self._tickets:
            self.pending_bytes -= ticket.reservation
            self._tickets.remove(ticket)

    def resolve(self, ticket, wait=True):
        self._check(ticket)
        if not ticket.acquired:
            raise ValueError("staging ticket has not been acquired")
        if ticket.end_event is not None:
            if wait:
                ticket.end_event.synchronize()
            elif not ticket.end_event.query():
                raise RuntimeError("resolve(wait=False) requires a completed transfer")
            ticket.timing["h2d_seconds"] = ticket.start_event.elapsed_time(ticket.end_event) / 1000.
        timing = ticket.timing
        self._release(ticket)
        return timing

    def cancel_all(self):
        tickets = list(self._tickets)
        for ticket in tickets:
            if ticket.future is not None:
                ticket.future.cancel()
        for ticket in tickets:
            if ticket.future is not None and not ticket.future.cancelled():
                try:
                    ticket.future.result()
                except Exception:
                    pass
        if self._stream is not None and tickets:
            self._stream.synchronize()
        for ticket in tickets:
            self._release(ticket)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.cancel_all()
        finally:
            if self._worker is not None:
                self._worker.shutdown(wait=True, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
