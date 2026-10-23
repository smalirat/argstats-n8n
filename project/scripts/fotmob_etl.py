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


# Columnas de stats_jugador_partido que salen de playerStats, en el orden del
# INSERT: (columna, clave de FotMob, "value" o "total" en las fracciones).
# Son las métricas que también publican otras fuentes; lo propio de FotMob va
# a la columna `fotmob` (ver stats_fotmob).
COLUMNAS_STATS = [
    ("tiros_al_arco", "ShotsOnTarget", "value"),
    ("tiros_bloqueados", "blocked_shots", "value"),
    ("tiros_al_palo", "shots_woodwork", "value"),
    ("xgot", "expected_goals_on_target_variant", "value"),
    ("npxg", "expected_goals_non_penalty", "value"),
    ("grandes_ocasiones_falladas", "big_chance_missed_title", "value"),
    ("pases_precisos", "accurate_passes", "value"),
    ("pases_intentados", "accurate_passes", "total"),
    ("pases_ultimo_tercio", "passes_into_final_third", "value"),
    ("pases_largos_precisos", "long_balls_accurate", "value"),
    ("pases_largos_intentados", "long_balls_accurate", "total"),
    ("centros_precisos", "accurate_crosses", "value"),
    ("centros_intentados", "accurate_crosses", "total"),
    ("ocasiones_creadas", "chances_created", "value"),
    ("grandes_ocasiones_creadas", "big_chance_created_team_title", "value"),
    ("toques", "touches", "value"),
    ("toques_area_rival", "touches_opp_box", "value"),
    ("regates_exitosos", "dribbles_succeeded", "value"),
    ("regates_intentados", "dribbles_succeeded", "total"),
    ("perdidas", "dispossessed", "value"),
    ("offsides", "Offsides", "value"),
    ("entradas", "matchstats.headers.tackles", "value"),
    ("intercepciones", "interceptions", "value"),
    ("despejes", "clearances", "value"),
    ("bloqueos", "shot_blocks", "value"),
    ("recuperaciones", "recoveries", "value"),
    ("regateado", "dribbled_past", "value"),
    ("duelos_ganados", "duel_won", "value"),
    ("duelos_perdidos", "duel_lost", "value"),
    ("duelos_terrestres_ganados", "ground_duels_won", "value"),
    ("duelos_terrestres_intentados", "ground_duels_won", "total"),
    ("duelos_aereos_ganados", "aerials_won", "value"),
    ("duelos_aereos_intentados", "aerials_won", "total"),
    ("faltas_cometidas", "fouls", "value"),
    ("faltas_recibidas", "was_fouled", "value"),
    ("penales_ganados", "penalties_won", "value"),
    ("penales_cometidos", "conceded_penalties", "value"),
    ("penales_errados", "missed_penalty", "value"),
    ("errores_gol", "errors_led_to_goal", "value"),
    ("goles_en_contra", "owngoal", "value"),
]

# Solo de arqueros: en el resto de los jugadores quedan en NULL (no aplica)
COLUMNAS_ARQUERO = [
    ("atajadas", "saves", "value"),
    ("goles_recibidos", "goals_conceded", "value"),
    ("xgot_recibido", "expected_goals_on_target_faced", "value"),
    ("penales_atajados", "saved_penalties", "value"),
]

# Claves que ya tienen columna propia o que se calculan con otras (no se
# repiten en `fotmob`)
CLAVES_CON_COLUMNA = {
    "minutes_played", "rating_title", "goals", "assists", "expected_goals", "expected_assists",
    "total_shots", "ShotsOffTarget",
    *(k for _, k, _ in COLUMNAS_STATS), *(k for _, k, _ in COLUMNAS_ARQUERO),
}
CLAVES_DERIVADAS = {"xg_and_xa", "shot_accuracy", "goals_prevented", None}


def stats_por_clave(player_data: dict[str, Any]) -> dict[str, dict]:
    """Todas las estadísticas de un jugador por su clave de FotMob (la primera vez que aparece)."""
    stats = {}

    for grupo in player_data.get("stats", []) or []:
        for stat_info in (grupo.get("stats") or {}).values() if isinstance(grupo, dict) else []:
            if isinstance(stat_info, dict):
                stats.setdefault(stat_info.get("key"), stat_info.get("stat") or {})

    return stats


def valor_stat(stats: dict[str, dict], clave: str, campo: str = "value", faltante=0):
    """
    FotMob no manda la estadística cuando vale 0 (un jugador que no pateó no
    trae ShotsOnTarget): si el jugador tiene estadísticas, lo que falta es 0.
    """
    stat = stats.get(clave)
    if stat is None:
        return faltante
    valor = stat.get(campo)
    return valor if valor is not None else faltante


def stats_fotmob(player_data: dict[str, Any], stats: dict[str, dict]) -> str | None:
    """JSON con lo propio de FotMob: lo que no tiene columna ni se calcula con otras."""
    propias = {}

    for clave, stat in stats.items():
        if clave in CLAVES_CON_COLUMNA or clave in CLAVES_DERIVADAS or "value" not in stat:
            continue
        propias[clave] = [stat["value"], stat["total"]] if "total" in stat else stat["value"]

    if player_data.get("isPotm"):
        propias["jugador_del_partido"] = True

    return json.dumps(propias) if propias else None


def tarjetas_por_jugador(eventos: list[dict]) -> dict[int, tuple[bool, bool]] | None:
    """
    (amarilla, roja) por jugador según los eventos del partido. La doble
    amarilla cuenta como amarilla y roja. None si el partido no tiene eventos:
    no se sabe quién fue amonestado.
    """
    if not eventos:
        return None

    tarjetas = {}

    for ev in eventos:
        if ev.get("type") != "Card":
            continue
        j_id = (ev.get("player") or {}).get("id") or ev.get("playerId")
        if not j_id:
            continue
        amarilla, roja = tarjetas.get(int(j_id), (False, False))
        carta = ev.get("card")
        tarjetas[int(j_id)] = (amarilla or carta in ("Yellow", "YellowRed"), roja or carta in ("Red", "YellowRed"))

    return tarjetas


# Eventos que se guardan cuando FotMob no les manda ID (los demás, como cambios
# o fin del tiempo, nunca traían ID y no se guardan)
TIPOS_EVENTO = {"Goal", "Card", "MissedPenalty"}


def id_evento_sintetico(partido_id: int, n: int) -> int:
    """ID negativo para el n-ésimo evento sin ID de un partido."""
    return -(partido_id * 1000 + n)


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

    eventos_obj = match_facts.get("events")
    eventos = eventos_obj.get("events", []) if isinstance(eventos_obj, dict) else []
    tarjetas = tarjetas_por_jugador(eventos)

    for p_id_str, p_data in (content.get("playerStats") or {}).items():
        try:
            jugador_id = int(p_id_str)
        except ValueError:
            continue

        stats = stats_por_clave(p_data)
        arquero = bool(p_data.get("isGoalkeeper"))
        # Total de tiros: FotMob a veces no manda total_shots, pero siempre es al arco + afuera + bloqueados
        tiros = valor_stat(stats, "total_shots", faltante=None)
        if tiros is None:
            tiros = sum(valor_stat(stats, k) for k in ("ShotsOnTarget", "ShotsOffTarget", "blocked_shots"))
        amarilla, roja = (None, None) if tarjetas is None else tarjetas.get(jugador_id, (False, False))

        filas["jugadores"].append((jugador_id, extraer_nombre(p_data.get("name"))))
        filas["stats"].append((
            partido_id,
            jugador_id,
            p_data.get("teamId"),
            valor_stat(stats, "minutes_played"),
            valor_stat(stats, "rating_title", faltante=None),
            valor_stat(stats, "goals"),
            valor_stat(stats, "assists"),
            valor_stat(stats, "expected_goals", faltante=0.0),
            valor_stat(stats, "expected_assists", faltante=0.0),
            amarilla,
            roja,
            tiros,
            *(valor_stat(stats, clave, campo) for _, clave, campo in COLUMNAS_STATS),
            *(valor_stat(stats, clave, campo) if arquero else None for _, clave, campo in COLUMNAS_ARQUERO),
            stats_fotmob(p_data, stats),
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

    sinteticos = 0
    for ev in eventos:
        ev_id = ev.get("eventId")
        if not ev_id:
            # Desde el Clausura 2026 FotMob manda eventId 0 en goles y tarjetas:
            # se les da un ID negativo estable por partido (no choca con los de
            # FotMob) y se borran y reinsertan en cada carga (ver cargar_partidos)
            if ev.get("type") not in TIPOS_EVENTO:
                continue
            sinteticos += 1
            ev_id = id_evento_sintetico(partido_id, sinteticos)

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
            # En el formato nuevo el gol en contra viene aparte (ownGoal), no en goalDescription
            ev.get("card") or ev.get("goalDescription") or ("Own goal" if ev.get("ownGoal") in (True, "True", "true") else None),
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
            t.get("situation"),
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

    # Mismo orden que la tupla de extraer_filas
    columnas_stats = [
        "minutos_jugados", "rating", "goles", "asistencias", "xg", "xa", "tarjeta_amarilla", "tarjeta_roja", "tiros",
        *(c for c, _, _ in COLUMNAS_STATS), *(c for c, _, _ in COLUMNAS_ARQUERO), "fotmob",
    ]
    insertar(f"""
        INSERT INTO stats_jugador_partido (partido_id, jugador_id, equipo_id, {", ".join(columnas_stats)})
        VALUES %s
        ON CONFLICT (partido_id, jugador_id) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in columnas_stats)}
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

    # Los eventos con ID sintético (negativo) se rehacen en cada carga: si
    # FotMob agrega o saca un evento, los números cambian y no deben duplicarse
    cur.execute(
        "DELETE FROM eventos WHERE evento_id < 0 AND partido_id = ANY(%s)",
        ([f["partido"]["partido_id"] for f in todas],),
    )

    insertar("""
        INSERT INTO eventos (evento_id, partido_id, equipo_id, jugador_id, minuto, tipo_evento, detalle)
        VALUES %s
        ON CONFLICT (evento_id) DO NOTHING
    """, sin_duplicados([e for f in todas for e in f["eventos"]], ultimo_gana=False))

    # Los tiros se actualizan (no DO NOTHING) para completar la situación de los ya cargados
    insertar("""
        INSERT INTO tiros (tiro_id, partido_id, equipo_id, jugador_id, minuto, x, y, xg, xgot, tipo_tiro, resultado, situacion)
        VALUES %s
        ON CONFLICT (tiro_id) DO UPDATE SET situacion = EXCLUDED.situacion
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
