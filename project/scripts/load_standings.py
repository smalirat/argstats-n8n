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
DEFAULT_LEAGUES_DIR = PROJECT_ROOT / ".runtime" / "leagues"

# ARGSTATS_DB_URL se lee de project/.env
load_dotenv(PROJECT_ROOT / ".env")


def log(message: str) -> None:
    # stdout queda reservado exclusivamente para JSON
    print(message, file=sys.stderr, flush=True)


def table_filter(include_prefix: str, exclude_prefix: str):
    # Para ligas de FotMob que juntan varias categorías (B Metro + Federal A)
    def incluir(tabla: str) -> bool:
        if include_prefix and not tabla.startswith(include_prefix):
            return False
        if exclude_prefix and tabla.startswith(exclude_prefix):
            return False
        return True

    return incluir


def load_standings(
    league_id: str,
    season: str,
    leagues_dir: Path,
    include_prefix: str,
    exclude_prefix: str,
) -> dict[str, Any]:

    db_url = os.getenv("ARGSTATS_DB_URL")

    if not db_url:
        raise RuntimeError(
            "ARGSTATS_DB_URL is not set (see .env.example)."
        )

    path = leagues_dir / f"league_{league_id}_{season}.json"

    if not path.exists():
        raise RuntimeError(
            f"League file not found: {path} (run fixtures first)."
        )

    league = json.loads(path.read_text(encoding="utf-8"))

    filas = fotmob_etl.filas_posiciones(
        league,
        table_filter(include_prefix, exclude_prefix),
    )

    if not filas:
        log("No standings tables for this league.")

        return {"leagueId": int(league_id), "rows": 0, "editions": []}

    engine = create_engine(db_url)

    try:
        with engine.begin() as conn:
            result = fotmob_etl.cargar_posiciones(conn, filas)

    finally:
        engine.dispose()

    log(f"Loaded {result['filas']} standings rows.")

    return {
        "leagueId": int(league_id),
        "rows": result["filas"],
        "editions": result["ediciones"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load FotMob league tables into the posiciones table."
    )

    parser.add_argument("--league-id", required=True)
    parser.add_argument("--season", required=True)

    parser.add_argument(
        "--leagues-dir",
        default=str(DEFAULT_LEAGUES_DIR),
    )

    parser.add_argument(
        "--include-table-prefix",
        default="",
        help="Only load tables whose name starts with this.",
    )

    parser.add_argument(
        "--exclude-table-prefix",
        default="",
        help="Skip tables whose name starts with this.",
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()

    try:
        result = load_standings(
            league_id=args.league_id,
            season=args.season,
            leagues_dir=Path(args.leagues_dir),
            include_prefix=args.include_table_prefix,
            exclude_prefix=args.exclude_table_prefix,
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
