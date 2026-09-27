"""OMAI Studio gateway — one WebSocket, one Claude Code turn at a time.

OMAI Studio (the video factory) runs the queue, the assets and the cost ledger.
For productions that have to be *built* rather than generated one clip at a
time, its panel opens a chat that talks to this process: the directing agent.

The contract with the panel is deliberately tiny, because it is the only thing
both sides must agree on:

    connect:  ws://openmontage:8010/ws?project=<tenant-slug>__<project-slug>
    client -> {"type": "message", "text": "..."}
    server -> {"type": "text",   "text": "..."}     what the agent says
              {"type": "log",    "text": "..."}     tools, costs, failures
              {"type": "status", "status": "..."}   one line of state

Three decisions worth knowing before changing anything here:

**The workspace name is never trusted.** Studio derives it from slugs it has
already checked against the session, but this port is reachable by anything on
the internal Docker network. A name that escapes ``OM_PROJECTS`` would let one
production read another's work, so it is validated against a whitelist pattern
and the resolved path is checked to still be a direct child.

**One turn at a time** (``OM_MAX_CONCURRENT``, default 1). Rendering opens
Chromium; two of those on an N100 with 16 GB take the panel down with them. A
second request waits, and is told that it is waiting.

**A turn that stops answering is killed** (``OM_TURN_TIMEOUT_S``). The agent is
spawned in its own process group precisely so the whole tree goes, not just the
CLI: a stranded Chromium holds a gigabyte for as long as the container lives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import time
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

log = logging.getLogger("omai.gateway")

PROJECTS_DIR = Path(os.environ.get("OM_PROJECTS", "/projects"))
OPENMONTAGE_DIR = Path(os.environ.get("OM_OPENMONTAGE", "/opt/openmontage"))
FACTORY_DIR = Path(os.environ.get("OM_FACTORY", "/factory"))
CLAUDE_BIN = os.environ.get("OM_CLAUDE_BIN", "claude")
CLAUDE_FLAGS = shlex.split(os.environ.get("OM_CLAUDE_FLAGS", ""))
MAX_CONCURRENT = max(1, int(os.environ.get("OM_MAX_CONCURRENT", "1")))
TURN_TIMEOUT_S = max(30, int(os.environ.get("OM_TURN_TIMEOUT_S", "1800")))

# A stream-json line carrying a long tool result can be megabytes; the asyncio
# default of 64 KiB would raise LimitOverrunError and lose the rest of the turn.
STDOUT_LIMIT = 8 * 1024 * 1024

# Slugs come from Studio as `<tenant>__<project>`, both already slugified there.
# Dots are excluded on purpose: no `..`, and no name that reads as a path.
WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,100}$")

CLOSE_BAD_PROJECT = 4400

# Close codes the panel already knows how to explain to a person. Reusing them
# means a gateway-side refusal reads the same as a Studio-side one.
CLOSE_UPSTREAM = 4502


# --------------------------------------------------------------------- workspace


class BadWorkspace(ValueError):
    """The project name would not stay inside the projects directory."""


def workspace_path(name: str, root: Path | None = None) -> Path:
    """Resolve a project name to its directory, or refuse.

    Two checks, not one. The pattern rejects the obvious (`../`, absolute
    paths, a name with a slash in it); resolving and re-checking the parent
    catches what the pattern cannot see, such as a symlink planted inside the
    projects volume that points somewhere else entirely.
    """
    root = (root or PROJECTS_DIR).resolve()
    if not WORKSPACE_RE.match(name or ""):
        raise BadWorkspace(f"nome di progetto non ammesso: {name!r}")
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise BadWorkspace(f"il progetto {name!r} uscirebbe da {root}")
    return candidate


GUIDA = """# Regia — {nome}

Questa e' la cartella di lavoro di una produzione di OMAI Studio. Tutto quello
che scrivi qui resta qui: e' il volume del progetto, ed e' l'unico posto in cui
puoi scrivere.

## Prima di rispondere

Leggi `openmontage/AGENT_GUIDE.md`. E' il contratto di OpenMontage: quali
strumenti esistono, come si sceglie una pipeline, come si passa da uno stadio
al successivo. Non e' un'introduzione da saltare — senza, sceglierai lo
strumento sbagliato.

## Dove sono le cose

| Percorso | Cos'e' |
|---|---|
| `.` | la produzione: qui si scrive |
| `openmontage/` | la piattaforma OpenMontage, in sola lettura |
| `factory/` | il materiale di OMAI Studio, **in sola lettura** |

## Il materiale della Factory

Sotto `factory/` c'e' quello che la Factory ha gia' prodotto e caricato, nella
forma `t<tenant>/p<progetto>/`. Sono gli asset di produzione: si leggono e si
usano negli shot, non si modificano. Se serve una variante, se ne fa una copia
qui dentro.

## Quello che non puoi fare da qui

Accodare job sui provider a pagamento della Factory. Quelli passano dal ledger
di Studio — si stima, si prenota, si riconcilia — e il pannello e' il posto da
cui si fa. Serve a sapere cosa costa ogni progetto: una spesa che nasce qui
sarebbe una riga che non c'e' in nessun conto.
"""


def prepare_workspace(path: Path, *, openmontage: Path | None = None,
                      factory: Path | None = None) -> Path:
    """Create the project directory and point the agent at its surroundings.

    The two symlinks are how the agent reaches the platform and the Factory's
    material without either being copied per project: on this machine that
    would be gigabytes per production. ``.claude`` is symlinked too, so the
    skills and the tool registry load in a workspace that is not the
    OpenMontage checkout.
    """
    openmontage = openmontage or OPENMONTAGE_DIR
    factory = factory or FACTORY_DIR
    path.mkdir(parents=True, exist_ok=True)

    collegamenti = {"openmontage": openmontage, "factory": factory,
                    ".claude": openmontage / ".claude"}
    for nome, destinazione in collegamenti.items():
        collegamento = path / nome
        if not destinazione.exists():
            continue
        # Un collegamento che punta altrove e' peggio di uno assente: l'agente
        # leggerebbe la piattaforma di un'immagine vecchia senza accorgersene.
        if collegamento.is_symlink() and Path(os.readlink(collegamento)) != destinazione:
            collegamento.unlink()
        if not collegamento.exists() and not collegamento.is_symlink():
            collegamento.symlink_to(destinazione, target_is_directory=True)

    guida = path / "CLAUDE.md"
    if not guida.exists():
        guida.write_text(GUIDA.format(nome=path.name), encoding="utf-8")
    (path / ".omai").mkdir(exist_ok=True)
    return path


def _sessione_file(path: Path) -> Path:
    return path / ".omai" / "sessione.json"


def leggi_sessione(path: Path) -> str | None:
    """The id of the conversation to resume, if we have one.

    It lives on the project volume and not in memory because the container is
    restarted by every redeploy: without it, each deploy would silently drop
    the agent back to turn one, and the person on the other side would be the
    one to notice.
    """
    try:
        return json.loads(_sessione_file(path).read_text(encoding="utf-8")).get("session_id") or None
    except (OSError, ValueError):
        return None


def scrivi_sessione(path: Path, session_id: str) -> None:
    try:
        _sessione_file(path).parent.mkdir(parents=True, exist_ok=True)
        _sessione_file(path).write_text(
            json.dumps({"session_id": session_id, "aggiornata": time.time()}), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disco pieno o volume in sola lettura
        log.warning("non ho potuto ricordare la sessione di %s: %s", path.name, exc)


# ------------------------------------------------------------------- traduzione


def _testo(blocco: dict) -> str:
    return str(blocco.get("text") or "").strip()


def traduci(evento: dict) -> list[dict]:
    """Turn one stream-json event into what the panel's chat can render.

    Written to tolerate a field that changes name rather than to match a
    schema: the CLI's output format has moved before and will move again, and
    the failure mode of being strict here is a turn that runs, costs money, and
    shows the person nothing.
    """
    tipo = evento.get("type")

    if tipo == "system" and evento.get("subtype") == "init":
        pezzi = [p for p in (evento.get("model"), f"{len(evento.get('tools') or [])} strumenti") if p]
        return [{"type": "log", "text": "sessione avviata · " + " · ".join(pezzi)}]

    if tipo == "assistant":
        fuori: list[dict] = []
        for blocco in (evento.get("message") or {}).get("content") or []:
            if not isinstance(blocco, dict):
                continue
            if blocco.get("type") == "text" and _testo(blocco):
                fuori.append({"type": "text", "text": _testo(blocco)})
            elif blocco.get("type") == "tool_use":
                fuori.append({"type": "log", "text": "→ " + str(blocco.get("name") or "strumento")})
        return fuori

    if tipo == "user":
        # I risultati degli strumenti non si mostrano: sono il grosso del volume
        # e quasi sempre rumore. I fallimenti si', perche' spiegano una risposta
        # che altrimenti sembra arbitraria.
        fuori = []
        for blocco in (evento.get("message") or {}).get("content") or []:
            if isinstance(blocco, dict) and blocco.get("type") == "tool_result" and blocco.get("is_error"):
                dettaglio = blocco.get("content")
                if isinstance(dettaglio, list):
                    dettaglio = " ".join(_testo(p) for p in dettaglio if isinstance(p, dict))
                fuori.append({"type": "log", "text": "strumento fallito: " + str(dettaglio or "")[:400]})
        return fuori

    if tipo == "result":
        pezzi = []
        if evento.get("duration_ms"):
            pezzi.append(f"{round(evento['duration_ms'] / 1000)}s")
        if evento.get("num_turns"):
            pezzi.append(f"{evento['num_turns']} passaggi")
        costo = evento.get("total_cost_usd")
        if costo is not None:
            # Il costo si dice sempre: e' un agente che chiama modelli a
            # pagamento, e un numero che nessuno vede non lo controlla nessuno.
            pezzi.append(f"${float(costo):.4f}")
        fuori = [{"type": "log", "text": "turno concluso" + (" · " + " · ".join(pezzi) if pezzi else "")}]
        if evento.get("is_error") or evento.get("subtype") not in (None, "success"):
            fuori.insert(0, {"type": "log",
                             "text": f"il turno si e' chiuso male ({evento.get('subtype') or 'errore'}): "
                                     + str(evento.get("result") or "")[:600]})
        return fuori

    return []


def id_sessione(evento: dict) -> str | None:
    valore = evento.get("session_id")
    return str(valore) if valore else None


# ----------------------------------------------------------------------- turno


def comando(testo: str, *, riprendi: str | None) -> list[str]:
    """The command line for one turn.

    ``--resume`` and not ``--continue``: continue picks the most recent
    conversation in the directory, which is the same thing right up until two
    productions share a volume or a turn is retried, and then it is a different
    thing without saying so.
    """
    argv = [CLAUDE_BIN, "-p", testo, "--output-format", "stream-json", "--verbose"]
    if riprendi:
        argv += ["--resume", riprendi]
    return argv + CLAUDE_FLAGS


class Regia:
    """The agent, one turn at a time."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT, timeout_s: int = TURN_TIMEOUT_S):
        self._posti = asyncio.Semaphore(max_concurrent)
        self._per_progetto: dict[str, asyncio.Lock] = {}
        self._timeout_s = timeout_s

    def _lucchetto(self, nome: str) -> asyncio.Lock:
        # Un lucchetto per progetto oltre al tetto globale: due turni nella
        # stessa cartella si sovrascriverebbero i file a vicenda, e la sessione
        # da riprendere diventerebbe quella sbagliata.
        return self._per_progetto.setdefault(nome, asyncio.Lock())

    def libero(self) -> bool:
        return not self._posti.locked()

    async def esegui(self, workspace: Path, testo: str, manda) -> None:
        """Run one turn, streaming everything the panel should see."""
        if not self.libero():
            await manda({"type": "status", "status": "in coda — l'agente sta lavorando a un altro turno"})
        async with self._posti, self._lucchetto(workspace.name):
            await manda({"type": "status", "status": "sto lavorando"})
            try:
                await self._esegui(workspace, testo, manda)
            finally:
                await manda({"type": "status", "status": "pronto"})

    async def _esegui(self, workspace: Path, testo: str, manda) -> None:
        argv = comando(testo, riprendi=leggi_sessione(workspace))
        processo = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STDOUT_LIMIT,
            # Gruppo di processi suo: al timeout va giu' tutto l'albero, non solo
            # la CLI. Un Chromium orfano tiene un giga finche' vive il container.
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(self._leggi(processo, workspace, manda), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            await manda({"type": "log", "text":
                         f"nessuna risposta dopo {self._timeout_s // 60} minuti: fermo il turno."})
            self._abbatti(processo)
            await processo.wait()
        except asyncio.CancelledError:
            self._abbatti(processo)
            raise
        else:
            errore = (await processo.stderr.read()).decode("utf-8", "replace").strip()
            if processo.returncode not in (0, None) and errore:
                # Senza questo, un turno che muore per una chiave mancante lascia
                # la chat muta e sembra che l'agente non abbia niente da dire.
                await manda({"type": "log", "text": f"l'agente e' uscito con {processo.returncode}: "
                                                    + errore[-600:]})

    async def _leggi(self, processo, workspace: Path, manda) -> None:
        assert processo.stdout is not None
        async for riga in processo.stdout:
            testo = riga.decode("utf-8", "replace").strip()
            if not testo:
                continue
            try:
                evento = json.loads(testo)
            except ValueError:
                # Non e' JSON: e' la CLI che parla in chiaro, di solito per dire
                # che qualcosa non va. Passarlo com'e' e' meglio che scartarlo.
                await manda({"type": "log", "text": testo[:600]})
                continue
            sessione = id_sessione(evento)
            if sessione:
                scrivi_sessione(workspace, sessione)
            for messaggio in traduci(evento):
                await manda(messaggio)
        await processo.wait()

    @staticmethod
    def _abbatti(processo) -> None:
        try:
            os.killpg(os.getpgid(processo.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # pragma: no cover
            processo.kill()


# -------------------------------------------------------------------- servizio


def avvisi_di_configurazione() -> Iterable[str]:
    """What the operator should know before wondering why nothing happens.

    An agent driven over a websocket has nobody to answer a permission prompt:
    without flags that decide in advance, it will refuse its own tools and read
    as broken rather than as careful. Better to say so on connect than to leave
    it to be discovered.
    """
    if not CLAUDE_FLAGS:
        yield ("OM_CLAUDE_FLAGS e' vuoto: da qui nessuno puo' rispondere a una richiesta "
               "di permesso, quindi l'agente si fermera' al primo strumento che ne chiede uno. "
               "Per lasciarlo agire: OM_CLAUDE_FLAGS=--dangerously-skip-permissions.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        yield "ANTHROPIC_API_KEY non e' impostata: nessun turno potra' partire."
    if not FACTORY_DIR.exists():
        yield f"{FACTORY_DIR} non e' montata: l'agente non vede il materiale della Factory."


def crea_app(regia: Regia | None = None) -> FastAPI:
    app = FastAPI(title="OMAI Studio — gateway di regia", docs_url=None, redoc_url=None)
    app.state.regia = regia or Regia()

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "progetti": str(PROJECTS_DIR), "libero": app.state.regia.libero()}

    @app.websocket("/ws")
    async def ws(socket: WebSocket, project: str = "") -> None:
        try:
            workspace = workspace_path(project)
        except BadWorkspace as exc:
            # Si accetta e poi si chiude: un rifiuto prima dell'handshake arriva
            # al pannello come "disconnesso", senza il motivo.
            await socket.accept()
            await socket.send_text(json.dumps({"type": "log", "text": str(exc)}))
            await socket.close(code=CLOSE_BAD_PROJECT, reason="progetto non ammesso")
            return

        await socket.accept()
        manda = _mandante(socket)
        try:
            prepare_workspace(workspace)
        except OSError as exc:
            await manda({"type": "log", "text": f"non riesco a preparare la cartella: {exc}"})
            await socket.close(code=CLOSE_UPSTREAM, reason="cartella non disponibile")
            return

        await manda({"type": "log", "text": f"regia su «{workspace.name}»"})
        for avviso in avvisi_di_configurazione():
            await manda({"type": "log", "text": avviso})
        await manda({"type": "status", "status": "pronto"})

        try:
            while True:
                grezzo = await socket.receive_text()
                try:
                    messaggio = json.loads(grezzo)
                except ValueError:
                    messaggio = {"type": "message", "text": grezzo}
                if messaggio.get("type") != "message":
                    continue
                testo = str(messaggio.get("text") or "").strip()
                if not testo:
                    continue
                await app.state.regia.esegui(workspace, testo, manda)
        except WebSocketDisconnect:
            return

    return app


def _mandante(socket: WebSocket):
    async def manda(messaggio: dict) -> None:
        await socket.send_text(json.dumps(messaggio, ensure_ascii=False))
    return manda


app = crea_app()


def main() -> None:  # pragma: no cover - avvio
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("OM_PORT", "8010")))


if __name__ == "__main__":  # pragma: no cover
    main()
