"""Foreground private Broker tunnel using one fixed Doppler SSH identity."""

import argparse
import json
import os
import resource
import socket
import subprocess
import time
from contextlib import contextmanager

import hp_shared_client as client


def _port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))


def _wait_ready(process, port):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        client.need(process.poll() is None, "tunnel_start_failed")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    client.need(False, "tunnel_start_failed")


@contextmanager
def project_tunnel(local_port=18588):
    client.need(type(local_port) is int and 1 <= local_port <= 65535, "invalid_local_port")
    _port_available(local_port)
    with client.private_identity(client.TUNNEL_KEY) as path:
        argv = client.ssh_arguments(path, "quota-broker-client", forwarding=True)
        argv[-1:-1] = [
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:18588",
        ]
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=client.ssh_environment(),
        )
        try:
            client.need(process.poll() is None, "tunnel_start_failed")
            _wait_ready(process, local_port)
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-port", type=int, default=18588)
    args = parser.parse_args()
    try:
        with project_tunnel(args.local_port) as process:
            print(
                json.dumps(
                    {
                        "status": "tunnel_started",
                        "endpoint": f"http://127.0.0.1:{args.local_port}",
                        "provider_calls": 0,
                    }
                ),
                flush=True,
            )
            return process.wait()
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - private SSH and Doppler diagnostics stay private
        print('{"status":"stopped","reason":"tunnel_start_failed","automatic_retry":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
