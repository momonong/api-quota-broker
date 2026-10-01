"""One bounded, sequential seven-provider smoke; never prints payloads or credentials.

The default plan mode is read-only and offline. Live mode creates one 5-minute,
config-wide read-only Doppler Service Token in process memory. Run it only after
the exact live scope has received formal approval.
"""

import argparse
import base64
import json
import os
import re
import secrets
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quota_broker.catalog import MODELS
from quota_broker.config import Capacity, Quota, load_gateway_config
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.nvidia import NoRedirect, doppler_resolver_from_token

PROJECT = "api-quota-broker"
CONFIG = "dev"
MODELS_BY_PROVIDER = {
    "nvidia": "google/gemma-4-31b-it",
    "google": "gemini-3.5-flash-lite",
    "cloudflare": "@cf/meta/llama-3.2-1b-instruct",
    "groq": "openai/gpt-oss-20b",
    "mistral": "mistral-small-latest",
    "openrouter": "liquid/lfm-2.5-2.6b:free",
    "ocrspace": "ocr.space/engine2",
}
SECRETS = {
    "NVIDIA_API_KEY",
    "GEMINI_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OPENROUTER_API_KEY",
    "OCRSPACE_API_KEY",
}
# Official Llama 3.2 1B rates: 2,457 input and 18,252 output neurons per
# million tokens. Thirty is a conservative local bound for this tiny prompt
# and 64 output tokens; it is not provider-reported usage.
CF_NEURON_BOUND = 30
GLYPHS = (
    ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
)


def synthetic_ocr_png() -> str:
    """Small generated test image containing black 'OK' on white, no user data."""
    scale, margin, gap = 4, 8, 2
    width = (10 + gap) * scale + 2 * margin
    height = 7 * scale + 2 * margin
    pixels = [[255] * width for _ in range(height)]
    for glyph_index, glyph in enumerate(GLYPHS):
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    for y in range(margin + row * scale, margin + (row + 1) * scale):
                        for x in range(
                            margin + (glyph_index * (5 + gap) + col) * scale,
                            margin + (glyph_index * (5 + gap) + col + 1) * scale,
                        ):
                            pixels[y][x] = 0
    scanlines = b"".join(b"\x00" + bytes(row) for row in pixels)

    def chunk(kind: bytes, value: bytes) -> bytes:
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


def cli() -> str:
    path = Path.home() / ".local" / "bin" / "doppler"
    if not path.is_file():
        raise RuntimeError("Doppler CLI unavailable")
    return str(path)


def metadata_names(binary: str) -> set[str]:
    command = [
        binary,
        "--no-read-env",
        "--no-check-version",
        "--attempts",
        "1",
        "--scope",
        str(Path(__file__).resolve().parents[1]),
        "secrets",
        "--only-names",
        "--json",
        "--project",
        PROJECT,
        "--config",
        CONFIG,
    ]
    result = subprocess.run(command, capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise RuntimeError("Doppler name metadata unavailable")
    names = json.loads(result.stdout)
    if not isinstance(names, (list, dict)):
        raise TypeError("Doppler name metadata invalid")
    return set(names)


def service_token(binary: str) -> str:
    command = [
        binary,
        "--no-read-env",
        "--no-check-version",
        "--attempts",
        "1",
        "--scope",
        str(Path(__file__).resolve().parents[1]),
        "configs",
        "tokens",
        "create",
        "broker-v1-smoke-" + secrets.token_hex(6),
        "--project",
        PROJECT,
        "--config",
        CONFIG,
        "--access",
        "read",
        "--max-age",
        "5m",
        "--plain",
    ]
    result = subprocess.run(command, capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise RuntimeError("Service Token creation failed or is uncertain; inspect Access metadata")
    return result.stdout.decode().strip()


def zero_priced_text_model(model: object) -> bool:
    pricing = model.get("pricing") if isinstance(model, dict) else None
    architecture = model.get("architecture") if isinstance(model, dict) else None

    def zero_price(value: object) -> bool:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return False
        try:
            price = Decimal(str(value))
        except InvalidOperation:
            return False
        return price.is_finite() and price == 0

    return bool(
        isinstance(pricing, dict)
        and {"prompt", "completion"}.issubset(pricing)
        and all(zero_price(value) for value in pricing.values())
        and isinstance(architecture, dict)
        and architecture.get("input_modalities") == ["text"]
        and architecture.get("output_modalities") == ["text"]
    )


def confirm_openrouter_free() -> None:
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open("https://openrouter.ai/api/v1/models", timeout=15) as response:
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise RuntimeError("OpenRouter catalog too large")
    models = json.loads(raw)["data"]
    model = next(
        (
            item
            for item in models
            if isinstance(item, dict) and item.get("id") == MODELS_BY_PROVIDER["openrouter"]
        ),
        None,
    )
    if not zero_priced_text_model(model):
        raise RuntimeError("OpenRouter pinned free model price is unverified")


def runtime_targets(config_path: Path, providers: tuple[str, ...] | None = None):
    now = datetime.now(UTC)
    selected = []
    config = load_gateway_config(config_path)
    for provider, model_id in MODELS_BY_PROVIDER.items():
        if providers is not None and provider not in providers:
            continue
        source = next(
            (
                target
                for target in config
                if target.provider == provider and target.model == model_id
            ),
            None,
        )
        if source is None or source.secret_ref is None:
            raise RuntimeError("v1 target missing from profile")
        scope = source.shared_concurrency_scope or provider + ":smoke"
        quotas = [
            Quota(scope + ":smoke-rpm", "requests", 2, "rolling_minute"),
            Quota(
                scope + ":smoke-rpd",
                "requests",
                2,
                "day",
                "America/Los_Angeles" if provider == "google" else "UTC",
            ),
        ]
        if provider == "cloudflare":
            quotas.append(Quota(scope + ":smoke-neurons", "neurons", 100, "day"))
        elif provider != "ocrspace":
            quotas.append(Quota(scope + ":smoke-tpm", "input_tokens", 4096, "rolling_minute"))
        selected.append(
            replace(
                source,
                enabled=True,
                free_eligible=True,
                billing_enabled=False,
                verified_at=now,
                expires_at=now + timedelta(minutes=5),
                source="user-attested free/no-card for bounded v1 smoke; official generic docs",
                quotas=tuple(quotas),
                max_output_tokens=1 if provider == "ocrspace" else 64,
                capacity=(
                    Capacity(
                        "short_renewable",
                        86_400,
                        now,
                        "https://developers.cloudflare.com/workers-ai/platform/pricing/",
                        "Cloudflare account daily free Neurons; plan user-attested",
                        now + timedelta(minutes=5),
                    )
                    if provider == "cloudflare"
                    else source.capacity
                ),
            )
        )
    return tuple(selected)


def check_prior_receipts(providers: tuple[str, ...], prior_dbs: list[Path]) -> None:
    """Refuse a second dispatch even when the first result is unknown."""
    if len(providers) < len(MODELS_BY_PROVIDER) and not prior_dbs:
        raise RuntimeError("provider subset requires prior receipt database")
    for path in prior_dbs:
        if not path.is_file():
            raise RuntimeError("prior receipt database unavailable")
        # SQLite URI mode=ro prevents a typo from creating a new empty database.
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            for table in ("gateway_tasks", "gateway_attempts"):
                rows = con.execute(
                    f"SELECT DISTINCT provider FROM {table} WHERE dispatched_at IS NOT NULL"
                )
                if any(provider in providers for (provider,) in rows):
                    raise RuntimeError("selected provider was already dispatched")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("gateway.example.json"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--provider", action="append", choices=tuple(MODELS_BY_PROVIDER))
    parser.add_argument("--prior-db", action="append", type=Path, default=[])
    args = parser.parse_args()
    providers = tuple(
        provider
        for provider in MODELS_BY_PROVIDER
        if args.provider is None or provider in args.provider
    )
    if args.provider is not None and len(providers) != len(args.provider):
        parser.error("duplicate --provider")
    targets = runtime_targets(args.config, providers)
    if not args.live:
        for target in targets:
            print(
                target.provider,
                target.model,
                MODELS[target.model].endpoint_template,
                target.secret_ref,
            )
        print("plan_only: no token, secret read, or provider request")
        return 0
    if args.db is None:
        parser.error("--live requires --db")
    if args.db.exists() or not args.db.name.startswith("v1-smoke-"):
        raise RuntimeError("new independent v1-smoke database required")
    if any(args.db.resolve() == prior.resolve() for prior in args.prior_db):
        raise RuntimeError("new database must differ from prior receipts")
    check_prior_receipts(providers, args.prior_db)
    key = secrets.token_bytes(32)
    if "openrouter" in providers:
        confirm_openrouter_free()
    binary = cli()
    needed_secrets = {target.secret_ref for target in targets}
    if "cloudflare" in providers:
        needed_secrets.add("CLOUDFLARE_ACCOUNT_ID")
    if not needed_secrets.issubset(metadata_names(binary)):
        raise RuntimeError("required Doppler secret names absent")
    # Exclusive durable claim precedes token creation and every provider POST.
    descriptor = os.open(args.db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    gateway = Gateway(args.db, targets, key, lambda _: "")
    token_started = time.monotonic()
    token = service_token(binary)
    resolver = doppler_resolver_from_token(token, PROJECT, CONFIG)
    gateway.secret_resolver = resolver
    unsuccessful = False
    for provider in providers:
        model_id = MODELS_BY_PROVIDER[provider]
        provider_timeout = 60 if provider == "nvidia" else 30
        if time.monotonic() - token_started > 300 - provider_timeout - 30:
            print(json.dumps({"state": "stopped", "reason": "service_token_expiry_guard"}))
            return 2
        task = {
            "request_key": "v1-smoke-" + provider,
            "capability": "ocr" if provider == "ocrspace" else "text_generation",
            "input": synthetic_ocr_png()
            if provider == "ocrspace"
            else "Reply with the single word OK.",
            "max_output_tokens": 1 if provider == "ocrspace" else 64,
            "provider": provider,
            "model": model_id,
            "neuron_bound": CF_NEURON_BOUND if provider == "cloudflare" else None,
        }
        try:
            result = gateway.run(task)
            text_ok = (
                bool(re.search(r"\bOK\b", result.get("answer", "").upper()))
                if provider == "ocrspace"
                else None
            )
            good = result["state"] == "completed" and text_ok is not False
            print(
                json.dumps(
                    {
                        "provider": provider,
                        "model": model_id,
                        "state": result["state"],
                        "http_status": result["http_status"],
                        "error_code": result["error_code"],
                        "attempts": len(result["attempts"]),
                        "usage_source": result["usage_source"],
                        "expected_text_ok": text_ok,
                    }
                )
            )
            unsuccessful |= not good
        except GatewayError as exc:
            print(
                json.dumps(
                    {
                        "provider": provider,
                        "model": model_id,
                        "state": "rejected",
                        "error_code": exc.code,
                    }
                )
            )
            unsuccessful = True
    return 2 if unsuccessful else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("smoke_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from exc
