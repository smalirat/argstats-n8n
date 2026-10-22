# ArgStats · Ingesta con n8n

Pipeline semanal que mantiene actualizada una base de datos del fútbol argentino: partidos, detalle de cada partido (alineaciones, estadísticas por jugador, eventos, tiros) y tablas de posiciones de cinco categorías.

Lo orquesta **n8n**, y el trabajo pesado lo hacen scripts de **Python**: scraping con un navegador automatizado y carga agrupada a **PostgreSQL en Supabase**.

| Categoría | Workflow |
|---|---|
| Liga Profesional (Apertura, Clausura y playoffs) | `argstats-liga-profesional.json` |
| Primera Nacional | `argstats-primera-nacional.json` |
| Primera B Metropolitana | `argstats-primera-b-metro.json` |
| Federal A | `argstats-federal-a.json` |
| Copa Argentina | `argstats-copa-argentina.json` |

## Cómo funciona

Los cinco workflows tienen el mismo flujo. Lo único que cambia es el nodo *Category Config*, donde se definen liga, temporada, filtro y tope por corrida.

```
Category Config ─► Fetch Season Fixtures ─► Load Standings ─► Parse Fixtures ─► Only finished games
  (liga, temporada,   (fixture + tablas,       (tablas → posiciones,  (filtra la
   filtro, tope)       1 pedido por liga)       sin pedidos extra)     categoría)
                                                                            │
Sync Summary ◄─ Load Into DB ◄─ Parse Download ◄─ Download Matches ◄─ Collect Match IDs ◄─ Check Sync State
  (resumen)      (carga a la base,                  (scraping, con      (más recientes      (1 consulta:
                  sin navegador)                     caché local)        primero, con tope)   qué falta)
```

1. **Fixture y tablas.** Un solo pedido por categoría. La respuesta se guarda y se reutiliza para cargar las posiciones.
2. **Qué falta.** Una consulta a la base devuelve los partidos terminados que no están cargados o están incompletos (sin resultado o sin detalle de jugadores).
3. **Bajada.** Un único navegador para toda la corrida, con pausas al azar entre pedidos. Cada partido se guarda como JSON. Si un partido terminado ya está guardado, no se vuelve a pedir.
4. **Carga.** Los JSON se convierten en filas y se escriben con un `INSERT … ON CONFLICT` multi-fila por tabla, en una sola transacción. Si algo falla, se reintenta partido por partido para aislar el que rompe.

## Decisiones técnicas

- **Scraping separado de la carga.** Si falla la base, los JSON quedan guardados y la próxima corrida los carga sin volver a pedirlos, así que a la fuente solo se le pide lo indispensable.
- **Carga agrupada.** La primera versión hacía ~350 consultas por partido (una por jugador, evento y tiro). Contra Supabase, a ~160 ms por consulta, eso daba ~1 minuto por partido. Agrupando por tabla son unas pocas consultas por corrida: 300 partidos se cargan en ~2 s.
- **IDs que cambian.** La fuente le cambia el ID a un partido cuando confirma su fecha. Para no duplicarlo, cuando llega un ID nuevo se reemplaza el partido sin jugar del mismo cruce y edición (ventana de ±21 días). Un partido con resultado nunca se borra.
- **Formatos que cambian.** La fuente pasó de `"Liga Profesional Clausura"` + ronda `"13"` a `"Liga Profesional Argentina"` + `"Clausura - Round 13"`. El ETL normaliza todo al formato histórico de la base, playoffs incluidos (`"Apertura - Quarter-finals"` → edición `… Apertura Playoff`, ronda `1/4`).
- **Dos categorías en una liga.** La fuente trae B Metro y Federal A juntos. Se separan según las tablas en las que figuran los equipos de cada partido (462 y 511 partidos en 2026, sin casos mixtos).
- **Backlog sin trabar lo nuevo.** Cada corrida baja como máximo 150 partidos, empezando por los más recientes, así que la actualización semanal no queda detrás de los pendientes viejos.

## Estructura

```
├── start-n8n.ps1               Levanta n8n con las rutas del proyecto
└── project/
    ├── workflows/              Un workflow de n8n por categoría (JSON exportado)
    ├── scripts/
    │   ├── fotmob_sync.py      Scraping: fixtures y detalle de partidos (sin base de datos)
    │   ├── load_matches.py     Carga de partidos (sin navegador)
    │   ├── load_standings.py   Carga de posiciones
    │   └── fotmob_etl.py       Normalización y escritura agrupada en PostgreSQL
    ├── requirements.txt
    └── .env.example
```

No se versionan:
- `data/`: la base de n8n, con credenciales e historial de ejecuciones.
- `project/.runtime/`: el perfil del navegador, los JSON descargados y los backups.
- `project/.venv/` y `.env`.

## Correrlo

Requiere Python 3.13 (nodriver todavía no anda con 3.14), Node.js con n8n (`npm i -g n8n`) y Chrome.

```powershell
cd project
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # ARGSTATS_DB_URL = connection string de PostgreSQL / Supabase
cd ..
```

Con n8n apagado, importar los workflows:

```powershell
$env:N8N_USER_FOLDER = "$PWD\data"; n8n import:workflow --separate --input=project\workflows
```

Levantar n8n, crear la credencial *Postgres account* apuntando a la misma base y ejecutar el workflow de una categoría:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-n8n.ps1     # http://localhost:5678
```

Los workflows comparten el perfil del navegador, así que se corren de a uno. Los *Schedule Trigger* están escalonados cada 30 minutos.

## Aviso

Proyecto personal con fines educativos. Los datos se obtienen de fuentes públicas y no se redistribuyen.
