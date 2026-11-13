import base64
import json
import sqlite3
import struct
import zlib

import pytest
from test_encrypted_api import client_for, encrypted_fixture
from test_execution_policy import configure
from test_local_jobs import Backend

from quota_broker.client import ClientError


def png(width=1, height=1):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * width * 3) * height))
        + chunk(b"IEND", b"")
    )


def body_for(case):
    task = {
        "request_key": "invalid-local",
        "capability": "text_generation",
        "execution_policy": "local_only",
        "input": {"messages": [{"role": "user", "content": "synthetic"}]},
        "max_output_tokens": 16,
    }
    if case == "output":
        task["max_output_tokens"] = 2048
    elif case == "top_p":
        task["options"] = {"top_p": 0}
    elif case == "stop_count":
        task["options"] = {"stop": ["a", "b", "c", "d", "e"]}
    elif case == "stop_length":
        task["options"] = {"stop": ["x" * 201]}
    elif case == "assistant_parts":
        task["input"]["messages"] = [
            {"role": "assistant", "content": [{"type": "text", "text": "test"}]}
        ]
    elif case == "part_count":
        task["input"]["messages"][0]["content"] = [{"type": "text", "text": "t"} for _ in range(9)]
    else:
        data = png(1025) if case == "large_png" else png()
        mime = "image/png"
        if case == "jpeg":
            mime, data = "image/jpeg", b"\xff\xd8\xff\xe0" + b"\0" * 32
        if case == "bad_png":
            data = data[:-8]
        task["input"]["messages"][0]["content"] = [
            {"type": "image", "mime_type": mime, "data": base64.b64encode(data).decode()}
        ]
    return task


@pytest.mark.parametrize(
    "case",
    [
        "output",
        "top_p",
        "stop_count",
        "stop_length",
        "assistant_parts",
        "part_count",
        "large_png",
        "jpeg",
        "bad_png",
    ],
)
def test_incompatible_worker_input_is_rejected_before_ownership(tmp_path, case):
    backend = Backend()
    with encrypted_fixture(tmp_path, backend=backend) as fixture:
        configure(fixture[6])
        policy = json.loads(fixture[6].read_text())
        for principal in policy["principals"]:
            if principal["project_id"] == "a":
                principal["limits"].update(max_input_bytes=2 * 1024 * 1024, max_output_tokens=4096)
        fixture[6].write_text(json.dumps(policy))
        with pytest.raises(ClientError) as error:
            client_for(tmp_path, fixture).submit(body_for(case))
        assert error.value.detail["error"] in {"local_unsupported", "invalid_request"}
        with sqlite3.connect(fixture[3].db) as con:
            assert con.execute("SELECT count(*) FROM gateway_project_objects").fetchone()[0] == 0
        assert backend.posts == fixture[5] == []


def test_valid_single_png_is_accepted_for_original_worker(tmp_path):
    backend = Backend()
    with encrypted_fixture(tmp_path, backend=backend) as fixture:
        configure(fixture[6])
        assert client_for(tmp_path, fixture).submit(body_for("valid_png"))["state"] == "queued"
