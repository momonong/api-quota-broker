"""Operator-only UNKNOWN slot receipt; never called by HTTP, queue or timers.

Use preview to identify the original reservation and evidence hash. Apply requires
an externally verified human message authorizing occupancy release. Credentials,
provider calls, result replay and budget refunds are outside this command.
"""

import argparse
import json
import os
import resource
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from quota_broker.core import Broker, BrokerError, utcnow


def existing_broker(database: str) -> Broker:
    path = Path(database)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise BrokerError("forbidden", "existing private ledger required")
    # An operator must not initialize/migrate/repair a missing database as a side
    # effect of inspecting a receipt. Only the two explicitly called methods run.
    broker = object.__new__(Broker)
    broker.db = str(path)
    broker.clock = utcnow
    return broker


def run(args: argparse.Namespace) -> dict:
    broker = existing_broker(args.database)
    with closing(
        sqlite3.connect(Path(broker.db).absolute().as_uri() + "?mode=ro", uri=True)
    ) as con:
        con.row_factory = sqlite3.Row
        present = con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='unknown_slot_acknowledgements' AND type='table'"
        ).fetchone()
        if present is None:
            raise BrokerError("unavailable", "deploy reviewed acknowledgement support first")
        if args.action == "preview":
            return broker._unknown_slot_evidence(con, args.reservation_id, args.project)
    if not args.human_message_id or not args.acknowledgement_id or not args.expected_sha256:
        raise BrokerError(
            "invalid_request", "verified authorization and reviewed evidence required"
        )
    return broker.acknowledge_unknown_slot(
        args.reservation_id,
        args.project,
        args.acknowledgement_id,
        args.human_message_id,
        args.expected_sha256,
    )


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "apply"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--reservation-id", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--human-message-id")
    parser.add_argument("--acknowledgement-id")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), sort_keys=True))
        return 0
    except (BrokerError, OSError, ValueError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "status": "stopped",
                    "error": exc.code if isinstance(exc, BrokerError) else "inspection_required",
                    "automatic_retry": False,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
