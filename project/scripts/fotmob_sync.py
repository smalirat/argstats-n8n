import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any

import nodriver as uc


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / ".runtime" / "chrome_profile"
DEFAULT_MATCHES_DIR = PROJECT_ROOT / ".runtime" / "matches"
DEFAULT_LEAGUES_DIR = PROJECT_ROOT / ".runtime" / "leagues"

FOTMOB_LEAGUE_URL = (
    "https://www.fotmob.com/api/data/leagues"
    "?id={league_id}&season={season}"
)

FOTMOB_MATCH_URL = (
    "https://www.fotmob.com/api/data/matchDetails"
    "?matchId={match_id}"
)


def log(message: str) -> None:
    # stdout queda reservado exclusivamente para JSON
    print(message, file=sys.stderr, flush=True)


async def read_json_page(page) -> dict[str, Any]:
    await asyncio.sleep(1)

    raw_text = await page.evaluate("document.body.innerText")

    if not raw_text:
        raise RuntimeError("FotMob returned an empty response.")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "FotMob did not return valid JSON."
        ) from exc

    if isinstance(data, dict):
        if data.get("code") == "TURNSTILE_REQUIRED":
            raise RuntimeError(
                "FotMob requested interactive verification."
            )

        if "Verification" in str(data):
            raise RuntimeError(
                "FotMob returned a verification response."
            )

    return data


def normalize_fixture(match: dict[str, Any]) -> dict[str, Any]:
    status = match.get("status") or {}
    home = match.get("home") or {}
    away = match.get("away") or {}

    return {
        "matchId": match.get("id"),
        "utcTime": status.get("utcTime"),
        "finished": status.get("finished", False),
        "started": status.get("started", False),
        "cancelled": status.get("cancelled", False),

        "home": {
            "id": home.get("id"),
            "name": home.get("name"),
            "score": home.get("score"),
        },

        "away": {
            "id": away.get("id"),
            "name": away.get("name"),
            "score": away.get("score"),
        },

        "round": match.get("round"),

        "pageUrl": (
            match.get("pageUrl")
            or match.get("pageURL")
        ),
    }


async def start_browser(
    profile_path: Path,
    headless: bool,
):
    profile_path.mkdir(parents=True, exist_ok=True)

    return await uc.start(
        user_data_dir=str(profile_path),
        headless=headless,
        sandbox=False,
        browser_args=[
            "--disable-gpu",
            "--no-first-run",
        ],
    )


def parse_leagues(value: str) -> list[tuple[str, str]]:
    """'112:2026,8965:2026' -> [('112', '2026'), ('8965', '2026')]"""
    leagues = []

    for part in value.split(","):
        part = part.strip()
        if not part:
            continue

        league_id, _, season = part.partition(":")

        if not league_id or not season:
            raise RuntimeError(
                f"Invalid league '{part}', expected id:season."
            )

        leagues.append((league_id.strip(), season.strip()))

    if not leagues:
        raise RuntimeError("No leagues supplied.")

    return leagues


def tables_by_team(data: dict[str, Any]) -> dict[str, list[str]]:
    """Nombre de las tablas en las que aparece cada equipo (para separar categorías)."""
    raw = (data.get("table") or [{}])[0] if isinstance(data.get("table"), list) else {}
    table_data = (raw or {}).get("data") or {}

    tables = table_data.get("tables") or (
        [{"leagueName": table_data.get("leagueName"), "table": table_data["table"]}]
        if table_data.get("table") else []
    )

    teams: dict[str, list[str]] = {}

    for table in tables:
        for row in (table.get("table") or {}).get("all", []) or []:
            teams.setdefault(str(row.get("id")), []).append(table.get("leagueName") or "")

    return teams


async def fetch_fixtures(
    leagues: list[tuple[str, str]],
    profile_path: Path,
    headless: bool,
    delay: float,
    leagues_dir: Path,
) -> dict[str, Any]:

    leagues_dir.mkdir(parents=True, exist_ok=True)

    browser = None
    summary = []
    all_matches = []

    try:
        # Un solo navegador para todas las ligas
        browser = await start_browser(
            profile_path,
            headless,
        )

        for index, (league_id, season) in enumerate(leagues):
            log(
                f"Fetching FotMob league={league_id}, "
                f"season={season}"
            )

            url = FOTMOB_LEAGUE_URL.format(
                league_id=league_id,
                season=season,
            )

            page = await browser.get(url)

            data = await read_json_page(page)

            matches = (
                data.get("fixtures", {}).get("allMatches", [])
                or data.get("leagueOverviewMatches", [])
            )

            if not isinstance(matches, list):
                raise RuntimeError(
                    f"FotMob response for league {league_id} "
                    "does not contain matches."
                )

            details = data.get("details") or {}

            # Se guarda la respuesta para cargar las posiciones sin volver a pedirla
            save_json(
                leagues_dir / f"league_{league_id}_{season}.json",
                data,
            )

            teams = tables_by_team(data)

            normalized = []

            for match in matches:
                if not isinstance(match, dict) or not match.get("id"):
                    continue

                fixture = normalize_fixture(match)
                home_id = str(fixture["home"]["id"])
                away_id = str(fixture["away"]["id"])

                normalized.append({
                    **fixture,
                    "leagueId": int(league_id),
                    "season": season,
                    "tables": sorted(
                        set(teams.get(home_id, []) + teams.get(away_id, []))
                    ),
                })

            log(f"Received {len(normalized)} matches.")

            summary.append({
                "id": int(league_id),
                "name": details.get("name"),
                "season": season,
                "count": len(normalized),
            })

            all_matches += normalized

            if delay > 0 and index < len(leagues) - 1:
                await asyncio.sleep(
                    random.uniform(delay, delay * 2)
                )

        return {
            "source": "fotmob",
            "leagues": summary,
            "count": len(all_matches),
            "matches": all_matches,
        }

    finally:
        if browser is not None:
            log("Closing browser.")
            browser.stop()
            await asyncio.sleep(1)


def is_finished_file(path: Path) -> bool:
    # Un JSON ya bajado de un partido terminado no cambia: no hace falta
    # volver a pedírselo a FotMob
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False

    status = (data.get("header") or {}).get("status") or {}

    return status.get("finished") is True


def save_json(path: Path, data: dict[str, Any]) -> None:
    # Se escribe a un temporal y se renombra para no dejar archivos a medias
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


async def download_matches(
    match_ids: list[str],
    matches_dir: Path,
    profile_path: Path,
    headless: bool,
    delay: float,
    force: bool,
) -> dict[str, Any]:

    matches_dir.mkdir(parents=True, exist_ok=True)

    cached = []
    pending = []

    for match_id in match_ids:
        path = matches_dir / f"match_{match_id}.json"

        if not force and is_finished_file(path):
            cached.append(match_id)
        else:
            pending.append(match_id)

    log(
        f"{len(cached)} matches already downloaded, "
        f"{len(pending)} to fetch."
    )

    downloaded = []
    failed = []
    browser = None

    try:
        if pending:
            # Un solo navegador para todo el run
            browser = await start_browser(
                profile_path,
                headless,
            )

        for index, match_id in enumerate(pending):
            log(
                f"[{index + 1}/{len(pending)}] "
                f"Fetching match {match_id}"
            )

            try:
                url = FOTMOB_MATCH_URL.format(
                    match_id=match_id
                )

                page = await browser.get(url)

                data = await read_json_page(page)

                save_json(
                    matches_dir / f"match_{match_id}.json",
                    data,
                )

                downloaded.append(match_id)

            except Exception as exc:
                log(
                    f"Match {match_id} failed: {exc}"
                )

                failed.append({
                    "matchId": match_id,
                    "stage": "download",
                    "error": str(exc),
                })

                # Si FotMob pide verificación, seguir solo empeora las cosas
                if "verification" in str(exc).lower():
                    log("Stopping: FotMob requested verification.")
                    break

            if (
                delay > 0
                and index < len(pending) - 1
            ):
                # Pausa con variación para no pedir a ritmo fijo
                await asyncio.sleep(
                    random.uniform(delay, delay * 2)
                )

    finally:
        if browser is not None:
            log("Closing browser.")
            browser.stop()
            await asyncio.sleep(1)

    return {
        "source": "fotmob",
        "requested": len(match_ids),
        "downloaded": downloaded,
        "cached": cached,
        "failed": failed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FotMob helper for n8n."
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    # -------------------------
    # fixtures
    # -------------------------

    fixtures_parser = subparsers.add_parser(
        "fixtures",
        help="Retrieve season fixtures.",
    )

    fixtures_parser.add_argument(
        "--leagues",
        required=True,
        help="Comma-separated id:season pairs, e.g. 112:2026,8965:2026.",
    )

    fixtures_parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Minimum delay between leagues (actual: delay to 2x delay).",
    )

    fixtures_parser.add_argument(
        "--leagues-dir",
        default=str(DEFAULT_LEAGUES_DIR),
    )

    fixtures_parser.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE),
    )

    fixtures_parser.add_argument(
        "--headless",
        action="store_true",
    )

    # -------------------------
    # download
    # -------------------------

    download_parser = subparsers.add_parser(
        "download",
        help="Download match details as JSON files.",
    )

    download_parser.add_argument(
        "--match-ids",
        default="",
        help="Comma-separated FotMob match IDs (may be empty).",
    )

    download_parser.add_argument(
        "--matches-dir",
        default=str(DEFAULT_MATCHES_DIR),
    )

    download_parser.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE),
    )

    download_parser.add_argument(
        "--headless",
        action="store_true",
    )

    download_parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Minimum delay between requests (actual: delay to 2x delay).",
    )

    download_parser.add_argument(
        "--force",
        action="store_true",
        help="Download again even if the finished match is already saved.",
    )

    return parser


async def run(
    args: argparse.Namespace,
) -> dict[str, Any]:

    if args.command == "fixtures":
        return await fetch_fixtures(
            leagues=parse_leagues(args.leagues),
            profile_path=Path(args.profile),
            headless=args.headless,
            delay=args.delay,
            leagues_dir=Path(args.leagues_dir),
        )

    if args.command == "download":

        match_ids = [
            value.strip()
            for value in args.match_ids.split(",")
            if value.strip()
        ]

        return await download_matches(
            match_ids=match_ids,
            matches_dir=Path(args.matches_dir),
            profile_path=Path(args.profile),
            headless=args.headless,
            delay=args.delay,
            force=args.force,
        )

    raise RuntimeError(
        f"Unknown command: {args.command}"
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        result = uc.loop().run_until_complete(
            run(args)
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
