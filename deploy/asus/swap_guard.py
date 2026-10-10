"""Fail before copying the queue key unless this service forbids cgroup swap.

Only bounded kernel metadata is read. This does not prove PID1 credential
storage, tmpfs page charging or protection against privileged host changes.
"""

import os
import stat
import sys

CGROUP = "/proc/self/cgroup"
SWAP = "/sys/fs/cgroup/system.slice/api-quota-broker.service/memory.swap.max"
EXPECTED = b"0::/system.slice/api-quota-broker.service"
FAILURE = "broker swap guard failed"


class GuardError(ValueError):
    def __init__(self) -> None:
        super().__init__(FAILURE)


def _read(path: str, bound: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GuardError
        result = bytearray()
        while True:
            chunk = os.read(descriptor, bound + 1 - len(result))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            if len(result) > bound:
                raise GuardError
    finally:
        os.close(descriptor)


def verify() -> None:
    # Exact membership rejects cgroup v1/hybrid, sibling/child services and
    # duplicate unified records. Caller input cannot choose another cgroup.
    if _read(CGROUP, 4096) not in (EXPECTED, EXPECTED + b"\n"):
        raise GuardError
    if _read(SWAP, 32) not in (b"0", b"0\n"):
        raise GuardError


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise GuardError
        verify()
    except (OSError, GuardError):
        # Failure is always explicit and contains no raw metadata or OS error.
        print(FAILURE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
