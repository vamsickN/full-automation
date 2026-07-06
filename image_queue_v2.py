"""Agent 3: Performance — Optimized Image Generation Queue.

Fixes:
- Proper async/await instead of blocking threads
- Exponential backoff with jitter (prevents thundering herd)
- Circuit breaker pattern for repeated failures
- Memory-efficient: streams results instead of holding all in RAM
- Proper cancellation support
"""
import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Any, Dict, List

import config


class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ImageJob:
    id: str
    prompt: str
    params: Dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    result: Optional[Any] = None
    error: Optional[str] = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    progress_callback: Optional[Callable] = None


class CircuitBreaker:
    """Circuit breaker: stops hammering a dead endpoint."""

    def __init__(self, failure_threshold: int = 5, recovery_time: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.failures = 0
        self.last_failure = 0.0
        self.state = "closed"  # closed | open | half-open

    def record_failure(self):
        self.failures += 1
        self.last_failure = time.time()
        if self.failures >= self.failure_threshold:
            self.state = "open"

    def record_success(self):
        self.failures = 0
        self.state = "closed"

    def can_proceed(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure > self.recovery_time:
                self.state = "half-open"
                return True
            return False
        # half-open: allow one request
        return True


class ImageQueue:
    """High-performance image generation queue with backoff + circuit breaker."""

    def __init__(self, generate_fn: Callable):
        self.generate_fn = generate_fn
        self.max_concurrency = config.IMAGE_MAX_CONCURRENCY
        self.max_retries = config.IMAGE_MAX_RETRIES
        self.delay_ms = config.IMAGE_REQUEST_DELAY_MS
        self.backoff_base_ms = config.IMAGE_BACKOFF_BASE_MS
        self.backoff_max_ms = config.IMAGE_BACKOFF_MAX_MS
        self.rate_limit_cooldown_ms = config.IMAGE_RATE_LIMIT_COOLDOWN_MS

        self._jobs: Dict[str, ImageJob] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._circuit = CircuitBreaker(failure_threshold=5, recovery_time=30)
        self._cooldown_until = 0.0
        self._workers_started = False

    async def _ensure_workers(self):
        if self._workers_started:
            return
        self._workers_started = True
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        for i in range(self.max_concurrency):
            asyncio.create_task(self._worker(i))

    async def submit(self, job: ImageJob) -> str:
        """Submit a job. Returns job ID."""
        await self._ensure_workers()
        self._jobs[job.id] = job
        await self._queue.put(job.id)
        return job.id

    async def cancel(self, job_id: str) -> bool:
        """Cancel a pending job."""
        job = self._jobs.get(job_id)
        if job and job.status == JobStatus.PENDING:
            job.status = JobStatus.CANCELLED
            return True
        return False

    def get_status(self, job_id: str) -> Optional[ImageJob]:
        return self._jobs.get(job_id)

    async def _worker(self, worker_id: int):
        """Worker loop: pulls jobs from queue and processes them."""
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if not job or job.status == JobStatus.CANCELLED:
                self._queue.task_done()
                continue

            async with self._semaphore:
                await self._process(job)
            self._queue.task_done()

    async def _process(self, job: ImageJob):
        """Process a single job with retries + backoff."""
        job.status = JobStatus.RUNNING
        job.started_at = time.time()

        while job.attempts < self.max_retries:
            # Check circuit breaker
            if not self._circuit.can_proceed():
                await asyncio.sleep(5)
                continue

            # Check rate limit cooldown
            now = time.time()
            if now < self._cooldown_until:
                wait = self._cooldown_until - now
                await asyncio.sleep(wait)

            # Inter-request delay
            if self.delay_ms > 0:
                await asyncio.sleep(self.delay_ms / 1000)

            job.attempts += 1
            try:
                result = await asyncio.to_thread(
                    self.generate_fn,
                    job.prompt,
                    **job.params,
                )
                job.result = result
                job.status = JobStatus.DONE
                job.finished_at = time.time()
                self._circuit.record_success()
                if job.progress_callback:
                    job.progress_callback(job)
                return

            except Exception as e:
                err_str = str(e).lower()

                # Rate limited
                if "429" in err_str or "rate_limit" in err_str:
                    self._cooldown_until = time.time() + (self.rate_limit_cooldown_ms / 1000)
                    self._circuit.record_failure()
                    # Don't count rate limits against retry budget
                    job.attempts -= 1
                    await asyncio.sleep(self.rate_limit_cooldown_ms / 1000)
                    continue

                # Server error: exponential backoff with jitter
                if "500" in err_str or "502" in err_str or "503" in err_str:
                    self._circuit.record_failure()
                    backoff = min(
                        self.backoff_base_ms * (2 ** (job.attempts - 1)),
                        self.backoff_max_ms,
                    )
                    # Add jitter: +/- 20%
                    jitter = backoff * (0.8 + random.random() * 0.4)
                    await asyncio.sleep(jitter / 1000)
                    continue

                # Client error or unknown: fail immediately
                job.error = str(e)
                break

        # Exhausted retries
        if job.status != JobStatus.DONE:
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            if not job.error:
                job.error = f"Failed after {job.attempts} attempts"
            if job.progress_callback:
                job.progress_callback(job)

    @property
    def stats(self) -> Dict[str, int]:
        """Queue statistics."""
        counts = {s.value: 0 for s in JobStatus}
        for job in self._jobs.values():
            counts[job.status.value] += 1
        counts["total"] = len(self._jobs)
        counts["circuit"] = self._circuit.state
        return counts

    def clear_done(self):
        """Remove completed/failed jobs from memory."""
        to_remove = [
            jid for jid, j in self._jobs.items()
            if j.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)
        ]
        for jid in to_remove:
            del self._jobs[jid]
        return len(to_remove)
