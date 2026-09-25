"""Gateway di regia: un websocket davanti a Claude Code, dentro il container OpenMontage.

Sta sulla rete interna e non ha porte pubbliche. Chi arriva qui e' gia' stato autenticato
dal pannello OMAI Studio, che fa da ponte.

Un progetto = una cartella sotto OM_PROJECTS = una sessione che continua nel tempo.

SUI PERMESSI
------------
Questo gateway NON disattiva le conferme di Claude Code. La bozza di partenza cablava
`--dangerously-skip-permissions` come default, cioe' un agente che approva da solo
qualunque comando, compresi quelli che toccano il filesystem e la rete del container.
Un default del genere significa che la modalita' piu' pericolosa e' quella che ottieni
se non leggi la configurazione, ed e' l'opposto di come va scelto un rischio.

Le opzioni si passano da `OM_CLAUDE_FLAGS`, che di suo e' vuoto. Chi vuole allentare i
permessi lo fa scrivendolo nella configurazione dello stack, con il proprio nome sopra:
resta possibile, ma diventa una decisione presa, non una eredita'.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import time
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

log = logging.getLogger("regia")

PROJECTS = Path(os.environ.get("OM_PROJECTS", "/projects"))

# Vuoto di proposito: vedi la nota sui permessi in cima al file.
FLAGS = shlex.split(os.environ.get("OM_CLAUDE_FLAGS", ""))

MODEL = os.environ.get("OM_CLAUDE_MODEL", "")
TURN_TIMEOUT_S = int(os.environ.get("OM_TURN_TIMEOUT_S", "1800"))

# Ogni turno puo' aprire Chromium per i render Remotion: su un N100 con 16 GB, due
# produzioni in parallelo si portano via la macchina e fanno morire anche la Factory.
MAX_CONCURRENT = int(os.environ.get("OM_MAX_CONCURRENT", "1"))

SAFE = re.compile(r"[^a-zA-Z0-9_-]")

app = FastAPI(title="OMAI - gateway di regia")
_gate = asyncio.Semaphore(MAX_CONCURRENT)

# Un solo turno per volta sullo stesso progetto: due processi nella stessa cartella si
# sovrascrivono i file a vicenda.
_project_locks: dict[str, asyncio.Lock] = {}


def project_dir(project: str) -> Path:
    safe = SAFE.sub("_", project)[:80] or "default"
    path = PROJECTS / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sessions_file() -> Path:
    return PROJECTS / ".sessions.json"


def load_sessions() -> dict[str, str]:
    """Le sessioni stanno su disco, non in memoria.

    Tenendole in RAM, come faceva la bozza, ogni riavvio del container faceva ripartire
    l'agente da zero: i file del progetto restavano, il filo del discorso no, e chi stava
    lavorando se ne accorgeva solo dalle risposte che non tornavano.
    """
    try:
        return json.loads(_sessions_file().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_session(project: str, session_id: str) -> None:
    sessions = load_sessions()
    sessions[project] = session_id
    tmp = _sessions_file().with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(sessions, indent=2))
        tmp.replace(_sessions_file())
    except OSError as exc:
        log.warning("sessione non salvata: %s", exc)


async def send(ws: WebSocket, **payload) -> None:
    with suppress(RuntimeError, WebSocketDisconnect):
        await ws.send_text(json.dumps(payload))


def build_command(message: str, resume: str | None) -> list[str]:
    cmd = ["claude", "-p", message, "--output-format", "stream-json", "--verbose", *FLAGS]
    if MODEL:
        cmd += ["--model", MODEL]
    if resume:
        cmd += ["--resume", resume]
    return cmd


async def run_turn(project: str, message: str, ws: WebSocket) -> None:
    if shutil.which("claude") is None:
        await send(ws, type="error", text="La CLI di Claude Code non e' installata nel container.")
        return

    lock = _project_locks.setdefault(project, asyncio.Lock())
    if lock.locked():
        await send(ws, type="log",
                   text="Un turno e' gia' in corso su questo progetto: aspetto che finisca.")

    async with lock, _gate:
        workdir = project_dir(project)
        cmd = build_command(message, load_sessions().get(project))
        started = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(_stream(proc, project, ws), timeout=TURN_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            await send(ws, type="error",
                       text=f"Turno interrotto dopo {TURN_TIMEOUT_S // 60} minuti.")
            return
        except asyncio.CancelledError:
            # Il cliente se n'e' andato: non lasciare un processo a macinare, e a
            # spendere, per una conversazione che non ascolta piu' nessuno.
            proc.kill()
            await proc.wait()
            raise

        code = await proc.wait()
        if code != 0:
            stderr = (await proc.stderr.read()).decode(errors="replace").strip()
            # Senza questo, un turno fallito chiudeva in silenzio e sembrava che
            # l'agente non avesse niente da dire.
            await send(ws, type="error",
                       text=f"L'agente e' uscito con codice {code}."
                            + (f"\n{stderr[-1500:]}" if stderr else ""))
        await send(ws, type="status", status="pronto",
                   secondi=round(time.monotonic() - started, 1))


async def _stream(proc: asyncio.subprocess.Process, project: str, ws: WebSocket) -> None:
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            await send(ws, type="log", text=line[:2000])
            continue

        kind = event.get("type")
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    await send(ws, type="text", text=block["text"])
                elif block.get("type") == "tool_use":
                    args = json.dumps(block.get("input", {}), ensure_ascii=False)
                    await send(ws, type="log", text=f"[{block.get('name')}] {args[:240]}")
        elif kind == "result":
            if event.get("session_id"):
                save_session(project, event["session_id"])
            # Il costo del turno lo dichiara la CLI. Senza mostrarlo, la chat sarebbe
            # l'unica parte del sistema che spende senza dire quanto.
            cost = event.get("total_cost_usd")
            if cost is not None:
                await send(ws, type="log", text=f"costo del turno: ${float(cost):.4f}")


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "claude": shutil.which("claude") is not None,
        "progetti": len(load_sessions()),
        "conferme_disattivate": any("skip-permissions" in f for f in FLAGS),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, project: str = "default") -> None:
    await ws.accept()
    workdir = project_dir(project)
    resumed = project in load_sessions()
    await send(ws, type="log",
               text=f"Agente pronto in {workdir}."
                    + (" Riprendo la sessione precedente." if resumed else " Sessione nuova."))

    current: asyncio.Task | None = None
    try:
        while True:
            message = json.loads(await ws.receive_text())
            kind = message.get("type")

            if kind == "cancel" and current and not current.done():
                current.cancel()
                await send(ws, type="log", text="Turno annullato.")
                continue

            text = (message.get("text") or "").strip()
            if kind != "message" or not text:
                continue

            current = asyncio.create_task(run_turn(project, text, ws))
            await current
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        if current and not current.done():
            current.cancel()
            with suppress(asyncio.CancelledError):
                await current


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
