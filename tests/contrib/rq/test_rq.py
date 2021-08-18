import os
import subprocess
import time

import pytest
import redis
import rq

from ddtrace import Pin
from ddtrace.contrib.rq import patch
from ddtrace.contrib.rq import unpatch
from tests.utils import override_config
from tests.utils import snapshot

from ..config import REDIS_CONFIG
from .jobs import job_add1
from .jobs import job_fail


# Span data which isn't static to ignore in the snapshots.
snapshot_ignores = ["meta.job.id", "meta.error.stack"]


@pytest.fixture()
def connection():
    yield redis.Redis(port=REDIS_CONFIG["port"])


@pytest.fixture()
def queue(connection):
    patch()
    try:
        q = rq.Queue("q", connection=connection)
        yield q
    finally:
        unpatch()


@pytest.fixture()
def sync_queue(connection):
    patch()
    try:
        sync_q = rq.Queue("sync-q", is_async=False, connection=connection)
        yield sync_q
    finally:
        unpatch()



@snapshot(ignores=snapshot_ignores)
def test_queue_enqueue(sync_queue):
    sync_queue.enqueue(job_add1, 1)


@snapshot(ignores=snapshot_ignores)
def test_queue_failing_job(sync_queue):
    with pytest.raises(Exception):
        sync_queue.enqueue(job_fail)


@snapshot(ignores=snapshot_ignores)
def test_sync_worker(queue):
    job = queue.enqueue(job_add1, 1)
    worker = rq.SimpleWorker([queue], connection=queue.connection)
    worker.work(burst=True)
    assert job.result == 2


@snapshot(ignores=snapshot_ignores)
def test_sync_worker_multiple_jobs(queue):
    jobs = []
    for i in range(3):
        jobs.append(queue.enqueue(job_add1, i))
    worker = rq.SimpleWorker([queue], connection=queue.connection)
    worker.work(burst=True)
    assert [job.result for job in jobs] == [1, 2, 3]


@snapshot(ignores=snapshot_ignores)
def test_sync_worker_config_service(queue):
    job = queue.enqueue(job_add1, 10)
    with override_config("rq_worker", dict(service="my-worker-svc")):
        worker = rq.SimpleWorker([queue], connection=queue.connection)
        worker.work(burst=True)
    assert job.result == 11


@snapshot(ignores=snapshot_ignores)
def test_sync_worker_pin_service(queue):
    job = queue.enqueue(job_add1, 10)
    worker = rq.SimpleWorker([queue], connection=queue.connection)
    Pin.override(worker, service="my-pin-svc")
    worker.work(burst=True)
    assert job.result == 11


@snapshot(ignores=snapshot_ignores)
def test_worker_failing_job(queue):
    queue.enqueue(job_fail)
    worker = rq.SimpleWorker([queue], connection=queue.connection)
    worker.work(burst=True)


@snapshot(ignores=snapshot_ignores)
def test_enqueue(queue):
    env = os.environ.copy()
    env["DD_TRACE_REDIS_ENABLED"] = "false"
    p = subprocess.Popen(["ddtrace-run", "rq", "worker", "q"], env=env)
    try:
        job = queue.enqueue(job_add1, 1)
        # Wait for job to complete.
        while job.result is None:
            time.sleep(0.01)
        time.sleep(1)
        assert job.result == 2
    finally:
        p.terminate()
