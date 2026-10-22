import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine

import fotmob_etl


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MATCHES_DIR = PROJECT_ROOT / ".runtime" / "matches"

# ARGSTATS_DB_URL se lee de project/.env
load_dotenv(PROJECT_ROOT / ".env")


def log(message: str) -> None:
    # stdout queda reservado exclusivamente para JSON
    print(message, file=sys.stderr, flush=True)


def read_payloads(
    match_ids: list[str],
    matches_dir: Path,
) -> tuple[dict[str, dict], list[dict[str, Any]]]:

    payloads = {}
    failed = []

    for match_id in match_ids:
        path = matches_dir / f"match_{match_id}.json"

        try:
            payloads[match_id] = json.loads(
                path.read_text(encoding="utf-8")
            )

        except Exception as exc:
            failed.append({
                "matchId": match_id,
                "stage": "read",
                "error": str(exc),
            })

    return payloads, failed


def load_matches(
    match_ids: list[str],
    matches_dir: Path,
) -> dict[str, Any]:

    db_url = os.getenv("ARGSTATS_DB_URL")

    if not db_url:
        raise RuntimeError(
            "ARGSTATS_DB_URL is not set (see .env.example)."
        )

    payloads, failed = read_payloads(match_ids, matches_dir)
    synced = []
    replaced = []

    if not payloads:
        return {
            "requested": len(match_ids),
            "synced": synced,
            "replaced": replaced,
            "failed": failed,
        }

    engine = create_engine(db_url)

    try:
        # Primero todo junto en una transacción: es lo más rápido
        try:
            with engine.begin() as conn:
                result = fotmob_etl.cargar_partidos(
                    conn,
                    list(payloads.values()),
                )

            synced = result["cargados"]
            replaced = result["reemplazados"]

            log(f"Loaded {len(synced)} matches in one transaction.")

        # Si algo falla, se reintenta partido por partido para aislar
        # el que rompe y no perder los demás
        except Exception as exc:
            log(f"Bulk load failed ({exc}). Retrying one by one.")

            for match_id, payload in payloads.items():
                try:
                    with engine.begin() as conn:
                        result = fotmob_etl.cargar_partidos(
                            conn,
                            [payload],
                        )

                    synced += result["cargados"]
                    replaced += result["reemplazados"]

                except Exception as match_exc:
                    log(f"Match {match_id} load failed: {match_exc}")

                    failed.append({
                        "matchId": match_id,
                        "stage": "insert",
                        "error": str(match_exc),
                    })

    finally:
        engine.dispose()

    loaded = {str(partido_id) for partido_id in synced}

    for match_id in payloads:
        already_failed = any(f["matchId"] == match_id for f in failed)

        if match_id not in loaded and not already_failed:
            failed.append({
                "matchId": match_id,
                "stage": "insert",
                "error": "Payload has no match data.",
            })

    if replaced:
        log(f"Replaced {len(replaced)} old unplayed rows: {replaced}")

    return {
        "requested": len(match_ids),
        "synced": synced,
        "replaced": replaced,
        "failed": failed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load downloaded FotMob matches into PostgreSQL."
    )

    parser.add_argument(
        "--match-ids",
        default="",
        help="Comma-separated FotMob match IDs (may be empty).",
    )

    parser.add_argument(
        "--matches-dir",
        default=str(DEFAULT_MATCHES_DIR),
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()

    match_ids = [
        value.strip()
        for value in args.match_ids.split(",")
        if value.strip()
    ]

    try:
        result = load_matches(
            match_ids,
            Path(args.matches_dir),
        )

    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)

    print(
        json.dumps(
            result,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
