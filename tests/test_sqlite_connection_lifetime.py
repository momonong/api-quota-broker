"""Idle queue and metadata traffic must not consume FDs while GC is delayed."""

import gc
from pathlib import Path

import pytest
from test_gateway import make_gateway, target, task
from test_queue import setup

from quota_broker.gateway import GatewayError

pytestmark = pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux FD accounting")


def fd_count():
    return len(list(Path("/proc/self/fd").iterdir()))


def test_idle_queue_does_not_depend_on_garbage_collection(tmp_path):
    _gateway, queue = setup(tmp_path)
    gc.collect()
    baseline = fd_count()
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(200):
            assert queue.tick("idle-worker") is None
        assert fd_count() <= baseline + 1
    finally:
        if enabled:
            gc.enable()
        gc.collect()


def test_metadata_success_and_not_found_release_connections(tmp_path):
    gateway = make_gateway(tmp_path, [target("groq", "groq", "openai/gpt-oss-20b")], [])
    gateway.run(task())
    gateway.discovery.coverage()
    gc.collect()
    baseline = fd_count()
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(40):
            gateway.status("fixture-task")
            gateway.recent()
            gateway.usage()
            gateway.diagnostics()
            gateway.discovery.coverage()
            gateway.routing.reset_health("groq")
            with pytest.raises(GatewayError):
                gateway.status("missing")
        assert fd_count() <= baseline + 1
    finally:
        if enabled:
            gc.enable()
        gc.collect()
