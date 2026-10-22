"""
ETL de partidos de FotMob para el sync de n8n.

Dos pasos separados:
- extraer_filas(data): convierte un JSON de matchDetails en filas por tabla
  (no toca la base).
- cargar_partidos(conn, payloads): junta las filas de todos los partidos y
  las escribe con un INSERT multi-fila por tabla. Así son unas pocas idas y
  vueltas a la base en vez de una por jugador/evento/tiro.

Todas las escrituras son upserts, así que cargar el mismo partido varias
veces no genera duplicados.
"""

import json
import re
from typing import Any

from psycopg2.extras import execute_values


# Margen para considerar que un partido sin jugar es el mismo que llega con
# otro ID (FotMob reprograma y le cambia el ID cuando confirma la fecha)
DIAS_REEMPLAZO = 21


def obtener_stat(
    player_data: dict[str, Any],
    category_key: str,
    target_stat_key: str,
    default=0,
):
    for grupo in player_data.get("stats", []) or []:
        if not isinstance(grupo, dict) or grupo.get("key") != category_key:
            continue

        for stat_info in (grupo.get("stats") or {}).values():
            if isinstance(stat_info, dict) and stat_info.get("key") == target_stat_key:
                valor = (stat_info.get("stat") or {}).get("value")
                return valor if valor is not None else default

    return default


def extraer_nombre(obj_name) -> str:
    # A veces viene como string y a veces como {"fullName": ...}
    if isinstance(obj_name, dict):
        return obj_name.get("fullName", "Desconocido")

    return str(obj_name) if obj_name else "Desconocido"


def ronda_playoff(texto: str) -> str | None:
    """'8th Finals' -> '1/8', 'Quarter-finals' -> '1/4', 'Semi-finals' -> '1/2', 'Final' -> 'final'."""
    texto = texto.strip().lower()
    match = re.fullmatch(r"(\d+)(st|nd|rd|th) finals", texto)

    if match:
        return f"1/{match.group(1)}"

    return {"quarter-finals": "1/4", "semi-finals": "1/2", "final": "final"}.get(texto)


def normalizar_ronda(ronda) -> tuple[str | None, str, bool]:
    """
    FotMob a veces manda la ronda con la etapa adelante. Devuelve
    (etapa, ronda, es_playoff), con la ronda en el formato que ya usa la base:
    "Clausura - Round 13"      -> ("Clausura", "13", False)
    "Apertura - 8th Finals"    -> ("Apertura", "1/8", True)
    "Clausura - Semi-finals"   -> ("Clausura", "1/2", True)
    Sin etapa adelante se deja como viene ("13", "1/4", "16th Finals" de la Copa).
    """
    ronda = str(ronda) if ronda is not None else "N/A"
    match = re.fullmatch(r"\s*(.+?)\s+-\s+(.+?)\s*", ronda)

    if not match:
        return None, ronda, False

    etapa, resto = match.group(1), match.group(2)
    numero = re.fullmatch(r"Round\s+(\d+)", resto)

    if numero:
        return etapa, numero.group(1), False

    playoff = ronda_playoff(resto)

    if playoff:
        return etapa, playoff, True

    return etapa, resto, False


def normalizar_torneo(nombre: str, etapa: str | None, es_playoff: bool = False) -> str:
    """
    Si la etapa viene en la ronda y no en el nombre, se la agrega, igual que
    como FotMob nombraba antes los torneos:
    "Liga Profesional Argentina" + "Clausura"          -> "Liga Profesional Clausura"
    "Liga Profesional Argentina" + "Clausura" (playoff) -> "Liga Profesional Clausura Playoff"
    Los playoffs van a su propia edición para no mezclarse con la fase regular.
    """
    if etapa and etapa.lower() not in nombre.lower():
        nombre = f"{nombre.removesuffix(' Argentina')} {etapa}"

    if es_playoff and "playoff" not in nombre.lower():
        nombre = f"{nombre} Playoff"

    return nombre


def extraer_lineup(lineups: dict[str, Any]) -> list[tuple[Any, dict, bool]]:
    """
    Devuelve (equipo_id, jugador, es_titular). FotMob tiene dos formatos:
    el viejo (lineup.lineup[].players/bench) y el actual
    (homeTeam/awayTeam con starters/subs).
    """
    jugadores = []

    for team in lineups.get("lineup", []) or []:
        t_id = team.get("teamId")

        for row in team.get("players", []) or []:
            jugadores += [(t_id, p, True) for p in row]

        jugadores += [(t_id, p, False) for p in team.get("bench", []) or []]

    for team_key in ("homeTeam", "awayTeam"):
        team = lineups.get(team_key) or {}
        t_id = team.get("id")

        jugadores += [(t_id, p, True) for p in team.get("starters") or []]
        jugadores += [(t_id, p, False) for p in team.get("subs") or []]

    return jugadores


def extraer_filas(data: dict[str, Any]) -> dict[str, Any] | None:
    """Filas de un partido por tabla, o None si el payload no trae datos de partido."""
    gen = data.get("general") or {}
    head = data.get("header") or {}
    status = head.get("status") or {}
    reason = status.get("reason") or {}
    content = data.get("content") or {}
    match_facts = content.get("matchFacts") or {}

    if not gen or not head or "teams" not in head:
        return None

    partido_id = int(gen["matchId"])
    fecha = gen.get("matchTimeUTCDate")
    etapa, ronda, es_playoff = normalizar_ronda(gen.get("matchRound", "N/A"))
    torneo = normalizar_torneo(gen.get("leagueName", "Desconocido"), etapa, es_playoff)
    edicion = (torneo, fecha[:4] if fecha else "Desc")

    local, visitante = head["teams"][0], head["teams"][1]
    loc_id, vis_id = local["id"], visitante["id"]

    penales_loc, penales_vis = None, None
    if "penalties" in reason:
        pens = reason["penalties"]
        penales_loc = pens[0] if len(pens) > 0 else None
        penales_vis = pens[1] if len(pens) > 1 else None

    filas = {
        "edicion": edicion,
        "equipos": [
            (loc_id, local["name"], local["name"]),
            (vis_id, visitante["name"], visitante["name"]),
        ],
        "jugadores": [],
        # edicion_id se completa en cargar_partidos, cuando ya existe la edición
        "partido": {
            "partido_id": partido_id,
            "equipo_local_id": loc_id,
            "equipo_visitante_id": vis_id,
            "fecha_utc": fecha,
            "ronda": ronda,
            "goles_local": local.get("score", 0),
            "goles_visitante": visitante.get("score", 0),
            "penales_local": penales_loc,
            "penales_visitante": penales_vis,
            "estado": reason.get("short", "N/A"),
            "metadata": json.dumps(content.get("stats", {})),
        },
        "stats": [],
        "alineaciones": [],
        "eventos": [],
        "tiros": [],
        "momentum": [],
    }

    for p_id_str, p_data in (content.get("playerStats") or {}).items():
        try:
            jugador_id = int(p_id_str)
        except ValueError:
            continue

        filas["jugadores"].append((jugador_id, extraer_nombre(p_data.get("name"))))
        filas["stats"].append((
            partido_id,
            jugador_id,
            p_data.get("teamId"),
            obtener_stat(p_data, "top_stats", "minutes_played", 0),
            obtener_stat(p_data, "top_stats", "rating_title", None),
            obtener_stat(p_data, "top_stats", "goals", 0),
            obtener_stat(p_data, "top_stats", "assists", 0),
            obtener_stat(p_data, "top_stats", "expected_goals", 0.0),
            obtener_stat(p_data, "top_stats", "expected_assists", 0.0),
            obtener_stat(p_data, "discipline", "yellow_cards", 0) > 0,
            obtener_stat(p_data, "discipline", "red_cards", 0) > 0,
        ))

    for t_id, p, es_titular in extraer_lineup(content.get("lineup") or {}):
        j_id = p.get("id")
        if not j_id:
            continue

        posicion = p.get("role") or p.get("positionId")
        filas["jugadores"].append((j_id, extraer_nombre(p.get("name"))))
        filas["alineaciones"].append((
            partido_id,
            t_id,
            j_id,
            es_titular,
            p.get("shirtNumber"),
            str(posicion) if posicion is not None else None,
        ))

    eventos_obj = match_facts.get("events")
    eventos = eventos_obj.get("events", []) if isinstance(eventos_obj, dict) else []

    for ev in eventos:
        ev_id = ev.get("eventId")
        if not ev_id:
            continue

        jugador = ev.get("player") or {}
        # FotMob manda id 0 cuando no sabe quién fue (pasa en el ascenso): queda en NULL
        j_id = jugador.get("id") or None
        if j_id:
            filas["jugadores"].append((j_id, extraer_nombre(jugador.get("name"))))

        filas["eventos"].append((
            ev_id,
            partido_id,
            loc_id if ev.get("isHome") else vis_id,
            j_id,
            ev.get("time"),
            ev.get("type"),
            ev.get("card") or ev.get("goalDescription"),
        ))

    for t in (content.get("shotmap") or {}).get("shots", []) or []:
        t_id = t.get("id")
        if not t_id:
            continue

        j_id = t.get("playerId") or None
        if j_id:
            filas["jugadores"].append((j_id, t.get("playerName", "Desc")))

        filas["tiros"].append((
            t_id,
            partido_id,
            t.get("teamId"),
            j_id,
            t.get("min"),
            t.get("x"),
            t.get("y"),
            t.get("expectedGoals"),
            t.get("expectedGoalsOnTarget"),
            t.get("shotType"),
            t.get("eventType"),
        ))

    momentum = ((match_facts.get("momentum") or {}).get("main") or {}).get("data") or []

    for m in momentum:
        filas["momentum"].append((partido_id, m.get("minute"), m.get("value")))

    return filas


def sin_duplicados(
    filas: list[tuple],
    clave=lambda fila: fila[0],
    ultimo_gana: bool = True,
) -> list[tuple]:
    """
    Un INSERT multi-fila con ON CONFLICT DO UPDATE falla si la misma clave
    aparece dos veces en el lote, así que se deduplica antes.
    """
    vistos = {}

    for fila in filas:
        k = clave(fila)
        if ultimo_gana or k not in vistos:
            vistos[k] = fila

    return list(vistos.values())


def primeras_dos(fila: tuple):
    return fila[:2]


def cargar_partidos(conn, payloads: list[dict[str, Any]]) -> dict[str, list[int]]:
    """
    Carga varios matchDetails en una sola pasada (dentro de la transacción de
    conn). Devuelve los partido_id cargados y los de filas viejas que se
    reemplazaron; los payloads sin datos de partido se ignoran.
    """
    todas = [f for f in (extraer_filas(p) for p in payloads) if f]

    if not todas:
        return {"cargados": [], "reemplazados": []}

    cur = conn.connection.cursor()

    def insertar(sql: str, filas: list[tuple], fetch: bool = False):
        if not filas:
            return []

        return execute_values(cur, sql, filas, page_size=1000, fetch=fetch)

    ediciones = insertar("""
        INSERT INTO ediciones (nombre_torneo, temporada) VALUES %s
        ON CONFLICT (nombre_torneo, temporada) DO UPDATE
            SET nombre_torneo = EXCLUDED.nombre_torneo
        RETURNING nombre_torneo, temporada, edicion_id
    """, sin_duplicados([f["edicion"] for f in todas], clave=primeras_dos), fetch=True)

    edicion_ids = {(torneo, temporada): ed_id for torneo, temporada, ed_id in ediciones}

    insertar("""
        INSERT INTO equipos (equipo_id, nombre_fuente, nombre_corto) VALUES %s
        ON CONFLICT (equipo_id) DO UPDATE SET
            nombre_fuente = EXCLUDED.nombre_fuente,
            nombre_corto = EXCLUDED.nombre_corto
    """, sin_duplicados([e for f in todas for e in f["equipos"]]))

    insertar("""
        INSERT INTO jugadores (jugador_id, nombre_completo) VALUES %s
        ON CONFLICT (jugador_id) DO UPDATE SET
            nombre_completo = EXCLUDED.nombre_completo
    """, sin_duplicados([j for f in todas for j in f["jugadores"]]))

    # En el update la metadata se mergea (||) para no pisar datos de otras
    # fuentes (ej. odds), y se refrescan fecha/ronda por si el partido se
    # cargó antes de jugarse.
    partidos = [
        (
            f["partido"]["partido_id"],
            edicion_ids[f["edicion"]],
            f["partido"]["equipo_local_id"],
            f["partido"]["equipo_visitante_id"],
            f["partido"]["fecha_utc"],
            f["partido"]["ronda"],
            f["partido"]["goles_local"],
            f["partido"]["goles_visitante"],
            f["partido"]["penales_local"],
            f["partido"]["penales_visitante"],
            f["partido"]["estado"],
            f["partido"]["metadata"],
        )
        for f in todas
    ]

    partidos = sin_duplicados(partidos)

    # Si FotMob le cambió el ID a un partido (pasa con los reprogramados),
    # el viejo queda como una fila sin jugar del mismo cruce y edición.
    # Se borra antes de insertar el nuevo para no tener el partido dos veces.
    # Solo se borran filas sin jugar, así que nunca se pierde un resultado.
    reemplazados = insertar(f"""
        DELETE FROM partidos o
        USING (VALUES %s) AS n(partido_id, edicion_id, equipo_local_id, equipo_visitante_id, fecha_utc)
        WHERE o.edicion_id = n.edicion_id
          AND o.equipo_local_id = n.equipo_local_id
          AND o.equipo_visitante_id = n.equipo_visitante_id
          AND o.partido_id <> n.partido_id
          AND (o.goles_local IS NULL OR o.estado = 'N/A')
          AND ABS(EXTRACT(EPOCH FROM (o.fecha_utc::text::timestamptz - n.fecha_utc::timestamptz)))
              <= {DIAS_REEMPLAZO} * 86400
        RETURNING o.partido_id
    """, [(p[0], p[1], p[2], p[3], p[4]) for p in partidos if p[4]], fetch=True)

    insertar("""
        INSERT INTO partidos (
            partido_id, edicion_id, equipo_local_id, equipo_visitante_id,
            fecha_utc, ronda, goles_local, goles_visitante,
            penales_local, penales_visitante, estado, metadata
        ) VALUES %s
        ON CONFLICT (partido_id) DO UPDATE SET
            edicion_id = EXCLUDED.edicion_id,
            estado = EXCLUDED.estado,
            goles_local = EXCLUDED.goles_local,
            goles_visitante = EXCLUDED.goles_visitante,
            penales_local = EXCLUDED.penales_local,
            penales_visitante = EXCLUDED.penales_visitante,
            fecha_utc = COALESCE(EXCLUDED.fecha_utc, partidos.fecha_utc),
            ronda = EXCLUDED.ronda,
            metadata = CASE
                WHEN jsonb_typeof(EXCLUDED.metadata) <> 'object' THEN partidos.metadata
                WHEN jsonb_typeof(partidos.metadata) = 'object' THEN partidos.metadata || EXCLUDED.metadata
                ELSE EXCLUDED.metadata
            END
    """, partidos)

    insertar("""
        INSERT INTO stats_jugador_partido (
            partido_id, jugador_id, equipo_id, minutos_jugados, rating,
            goles, asistencias, xg, xa, tarjeta_amarilla, tarjeta_roja
        ) VALUES %s
        ON CONFLICT (partido_id, jugador_id) DO UPDATE SET
            minutos_jugados = EXCLUDED.minutos_jugados,
            rating = EXCLUDED.rating,
            goles = EXCLUDED.goles,
            asistencias = EXCLUDED.asistencias,
            xg = EXCLUDED.xg,
            xa = EXCLUDED.xa,
            tarjeta_amarilla = EXCLUDED.tarjeta_amarilla,
            tarjeta_roja = EXCLUDED.tarjeta_roja
    """, sin_duplicados([s for f in todas for s in f["stats"]], clave=primeras_dos))

    # Las tablas con DO NOTHING se quedan con la primera aparición, igual
    # que si se insertaran una por una.
    insertar("""
        INSERT INTO alineaciones (partido_id, equipo_id, jugador_id, es_titular, dorsal, posicion)
        VALUES %s
        ON CONFLICT (partido_id, jugador_id) DO NOTHING
    """, sin_duplicados(
        [a for f in todas for a in f["alineaciones"]],
        clave=lambda a: (a[0], a[2]),
        ultimo_gana=False,
    ))

    insertar("""
        INSERT INTO eventos (evento_id, partido_id, equipo_id, jugador_id, minuto, tipo_evento, detalle)
        VALUES %s
        ON CONFLICT (evento_id) DO NOTHING
    """, sin_duplicados([e for f in todas for e in f["eventos"]], ultimo_gana=False))

    insertar("""
        INSERT INTO tiros (tiro_id, partido_id, equipo_id, jugador_id, minuto, x, y, xg, xgot, tipo_tiro, resultado)
        VALUES %s
        ON CONFLICT (tiro_id) DO NOTHING
    """, sin_duplicados([t for f in todas for t in f["tiros"]], ultimo_gana=False))

    insertar("""
        INSERT INTO momentum (partido_id, minuto, valor) VALUES %s
        ON CONFLICT (partido_id, minuto) DO NOTHING
    """, sin_duplicados([m for f in todas for m in f["momentum"]], clave=primeras_dos, ultimo_gana=False))

    return {
        "cargados": [f["partido"]["partido_id"] for f in todas],
        "reemplazados": [r[0] for r in reemplazados],
    }


# ----------------------------------------------------------------------
# Posiciones
# ----------------------------------------------------------------------

# Nombres de liga que FotMob usa distinto en la tabla y en los partidos
ALIAS_LIGA = {
    "Primera B Nacional": "Primera Nacional",
}


def tablas_de_liga(league: dict[str, Any]) -> list[dict[str, Any]]:
    """Las tablas de la respuesta de /api/data/leagues: [{"leagueName", "table": {"all": [...]}}]."""
    tabla = (league.get("table") or [{}])[0] if isinstance(league.get("table"), list) else {}
    data = (tabla or {}).get("data") or {}

    if data.get("tables"):
        return data["tables"]

    if data.get("table"):
        return [{"leagueName": data.get("leagueName"), "table": data["table"]}]

    return []


def edicion_de_tabla(liga: str, tabla: str | None) -> tuple[str, str | None]:
    """
    (edición, zona) de una tabla, con el mismo criterio que los partidos:
    "Liga Profesional" + "Clausura Group A"        -> ("Liga Profesional Clausura", "Group A")
    "Primera B Nacional" + "Zona A"                -> ("Primera Nacional", "Zona A")
    "... & Torneo Federal A" + "Federal A Zona A"  -> ("Federal A Zona A", None)
    """
    liga = ALIAS_LIGA.get(liga, liga)
    tabla = re.sub(r"\s+", " ", tabla).strip() if tabla else None

    if not tabla:
        return liga, None

    etapa = re.fullmatch(r"(Apertura|Clausura)\s*(?:-\s*)?(.+)", tabla)
    if etapa:
        return normalizar_torneo(liga, etapa.group(1)), etapa.group(2)

    # Una zona suelta pertenece a la liga; cualquier otro nombre es su propia competición
    if re.fullmatch(r"(Zona|Group|Grupo)\s+\S+", tabla):
        return liga, tabla

    return tabla, None


def filas_posiciones(league: dict[str, Any], incluir=lambda tabla: True) -> list[dict[str, Any]]:
    details = league.get("details") or {}
    liga = details.get("name") or "Desconocido"
    temporada = details.get("selectedSeason")

    filas = []

    for tabla in tablas_de_liga(league):
        nombre_tabla = tabla.get("leagueName")
        if not incluir(nombre_tabla or ""):
            continue

        edicion, zona = edicion_de_tabla(liga, nombre_tabla)

        for equipo in (tabla.get("table") or {}).get("all", []) or []:
            if not equipo.get("id"):
                continue

            goles = str(equipo.get("scoresStr", "0-0")).split("-")

            filas.append({
                "edicion": (edicion, temporada),
                "equipo": (equipo["id"], equipo.get("name", "Desconocido"), equipo.get("shortName") or equipo.get("name")),
                "fila": (
                    zona,
                    equipo.get("idx"),
                    equipo.get("played", 0),
                    equipo.get("wins", 0),
                    equipo.get("draws", 0),
                    equipo.get("losses", 0),
                    int(goles[0]) if goles[0].isdigit() else 0,
                    int(goles[1]) if len(goles) > 1 and goles[1].isdigit() else 0,
                    equipo.get("goalConDiff", 0),
                    equipo.get("pts", 0),
                ),
            })

    return filas


def cargar_posiciones(conn, filas: list[dict[str, Any]]) -> dict[str, Any]:
    """Upsert de posiciones (una fila por equipo y edición)."""
    if not filas:
        return {"filas": 0, "ediciones": []}

    cur = conn.connection.cursor()

    ediciones = execute_values(cur, """
        INSERT INTO ediciones (nombre_torneo, temporada) VALUES %s
        ON CONFLICT (nombre_torneo, temporada) DO UPDATE
            SET nombre_torneo = EXCLUDED.nombre_torneo
        RETURNING nombre_torneo, temporada, edicion_id
    """, sin_duplicados([f["edicion"] for f in filas], clave=primeras_dos), fetch=True)

    edicion_ids = {(torneo, temporada): ed_id for torneo, temporada, ed_id in ediciones}

    execute_values(cur, """
        INSERT INTO equipos (equipo_id, nombre_fuente, nombre_corto) VALUES %s
        ON CONFLICT (equipo_id) DO NOTHING
    """, sin_duplicados([f["equipo"] for f in filas]))

    execute_values(cur, """
        INSERT INTO posiciones (
            edicion_id, equipo_id, zona, posicion, partidos_jugados,
            victorias, empates, derrotas, goles_favor, goles_contra, diferencia_gol, puntos
        ) VALUES %s
        ON CONFLICT (edicion_id, equipo_id) DO UPDATE SET
            zona = EXCLUDED.zona,
            posicion = EXCLUDED.posicion,
            partidos_jugados = EXCLUDED.partidos_jugados,
            victorias = EXCLUDED.victorias,
            empates = EXCLUDED.empates,
            derrotas = EXCLUDED.derrotas,
            goles_favor = EXCLUDED.goles_favor,
            goles_contra = EXCLUDED.goles_contra,
            diferencia_gol = EXCLUDED.diferencia_gol,
            puntos = EXCLUDED.puntos
    """, sin_duplicados(
        [(edicion_ids[f["edicion"]], f["equipo"][0], *f["fila"]) for f in filas],
        clave=primeras_dos,
    ), page_size=1000)

    return {
        "filas": len(filas),
        "ediciones": sorted({f"{n} {t}" for n, t in edicion_ids}),
    }
