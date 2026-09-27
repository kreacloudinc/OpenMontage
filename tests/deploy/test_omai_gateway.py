"""The OMAI Studio gateway: what it refuses, and what it shows.

Two things are worth a test here and the rest is plumbing.

The first is the workspace name. It arrives over a websocket, and although
Studio derives it from slugs it has already checked, this port is reachable by
anything on the internal network: a name that escapes the projects directory
lets one production read another's work.

The second is the translation of the agent's output. Its shape has changed
before and will change again, and the failure mode of being strict is a turn
that runs, costs money, and shows the person nothing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

GATEWAY = Path(__file__).resolve().parents[2] / "deploy" / "omai-studio" / "gateway.py"


def _carica():
    # Il gateway non e' un pacchetto importabile: e' un programma dentro
    # l'immagine. Si carica dal percorso, cosi' il test prova il file vero.
    spec = importlib.util.spec_from_file_location("omai_gateway", GATEWAY)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


gateway = _carica()


# ------------------------------------------------------------------ workspace


@pytest.mark.parametrize("nome", [
    "omai__ortopop-pilota",
    "omai__pilota",
    "a",
    "spazio_1__progetto-2",
])
def test_i_nomi_buoni_passano(tmp_path, nome):
    assert gateway.workspace_path(nome, root=tmp_path) == (tmp_path / nome).resolve()


@pytest.mark.parametrize("nome", [
    "..",
    "../altro",
    "omai/../../etc",
    "/etc/passwd",
    "omai__pilota/../../root",
    "",
    "MAIUSCOLO",
    "con spazio",
    "punto.punto",
    "x" * 200,
])
def test_i_nomi_che_uscirebbero_dalla_cartella_vengono_rifiutati(tmp_path, nome):
    """Il pattern ferma l'ovvio; il controllo sul padre ferma il resto."""
    with pytest.raises(gateway.BadWorkspace):
        gateway.workspace_path(nome, root=tmp_path)


def test_un_collegamento_piantato_nel_volume_non_porta_fuori(tmp_path):
    """Il nome puo' essere ineccepibile e il percorso no: basta che dentro il
    volume dei progetti ci sia un collegamento che punta altrove. Il pattern non
    lo vede, risolvere il percorso si'."""
    altrove = tmp_path / "altrove"
    altrove.mkdir()
    progetti = tmp_path / "progetti"
    progetti.mkdir()
    (progetti / "finto").symlink_to(altrove, target_is_directory=True)

    with pytest.raises(gateway.BadWorkspace):
        gateway.workspace_path("finto", root=progetti)


def test_la_cartella_nasce_con_i_collegamenti_e_la_guida(tmp_path):
    piattaforma = tmp_path / "openmontage"
    (piattaforma / ".claude").mkdir(parents=True)
    factory = tmp_path / "factory"
    factory.mkdir()
    workspace = tmp_path / "progetti" / "omai__pilota"

    gateway.prepare_workspace(workspace, openmontage=piattaforma, factory=factory)

    assert (workspace / "openmontage").resolve() == piattaforma.resolve()
    assert (workspace / "factory").resolve() == factory.resolve()
    assert (workspace / ".claude").resolve() == (piattaforma / ".claude").resolve()
    # La guida e' l'unica cosa che fa leggere AGENT_GUIDE.md prima di rispondere.
    assert "AGENT_GUIDE.md" in (workspace / "CLAUDE.md").read_text(encoding="utf-8")


def test_preparare_due_volte_non_rompe_niente(tmp_path):
    """Ci si passa a ogni connessione: se non fosse ripetibile, la seconda volta
    che si apre la chat il progetto sarebbe inutilizzabile."""
    piattaforma = tmp_path / "openmontage"
    piattaforma.mkdir()
    factory = tmp_path / "factory"
    factory.mkdir()
    workspace = tmp_path / "omai__pilota"

    gateway.prepare_workspace(workspace, openmontage=piattaforma, factory=factory)
    (workspace / "lavoro.txt").write_text("gia' fatto", encoding="utf-8")
    gateway.prepare_workspace(workspace, openmontage=piattaforma, factory=factory)

    assert (workspace / "lavoro.txt").read_text(encoding="utf-8") == "gia' fatto"


def test_un_collegamento_che_punta_altrove_viene_rifatto(tmp_path):
    """Dopo un aggiornamento dell'immagine la piattaforma puo' stare altrove. Un
    collegamento vecchio farebbe leggere all'agente la versione precedente senza
    che niente lo dica."""
    vecchia = tmp_path / "vecchia"
    vecchia.mkdir()
    nuova = tmp_path / "nuova"
    nuova.mkdir()
    factory = tmp_path / "factory"
    factory.mkdir()
    workspace = tmp_path / "omai__pilota"

    gateway.prepare_workspace(workspace, openmontage=vecchia, factory=factory)
    gateway.prepare_workspace(workspace, openmontage=nuova, factory=factory)

    assert (workspace / "openmontage").resolve() == nuova.resolve()


# ------------------------------------------------------------------- sessione


def test_la_sessione_sopravvive_al_riavvio_del_container(tmp_path):
    workspace = tmp_path / "omai__pilota"
    (workspace / ".omai").mkdir(parents=True)

    assert gateway.leggi_sessione(workspace) is None
    gateway.scrivi_sessione(workspace, "sess-123")
    assert gateway.leggi_sessione(workspace) == "sess-123"


def test_una_sessione_illeggibile_vale_come_nessuna(tmp_path):
    """Meglio ricominciare la conversazione che non partire: un file rovinato da
    un riavvio a meta' scrittura non deve rendere muto il progetto per sempre."""
    workspace = tmp_path / "omai__pilota"
    (workspace / ".omai").mkdir(parents=True)
    (workspace / ".omai" / "sessione.json").write_text("{non json", encoding="utf-8")

    assert gateway.leggi_sessione(workspace) is None


def test_si_riprende_la_sessione_per_id_e_non_l_ultima_della_cartella():
    """`--continue` prende la conversazione piu' recente in quella cartella: e' la
    stessa cosa finche' un turno non viene ritentato, e da li' in poi e' un'altra
    senza dirlo."""
    argv = gateway.comando("ciao", riprendi="sess-9")
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sess-9"
    assert "--continue" not in argv

    assert "--resume" not in gateway.comando("ciao", riprendi=None)


def test_il_comando_chiede_lo_stream_json():
    """Il pannello mostra quello che succede mentre succede. Senza stream-json la
    chat resterebbe ferma per tutto il turno e poi sputerebbe tutto insieme."""
    argv = gateway.comando("ciao", riprendi=None)
    assert argv[1:3] == ["-p", "ciao"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "stream-json"


# ----------------------------------------------------------------- traduzione


def test_quello_che_dice_l_agente_arriva_come_testo():
    fuori = gateway.traduci({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Faccio tre scene."}]},
    })
    assert fuori == [{"type": "text", "text": "Faccio tre scene."}]


def test_gli_strumenti_si_vedono_ma_non_i_loro_risultati():
    """Un turno di montaggio sposta megabyte di risultati: mostrarli seppellisce
    quello che l'agente sta dicendo. Il nome dello strumento basta a far capire
    che si sta muovendo qualcosa."""
    fuori = gateway.traduci({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Genero la scena."},
            {"type": "tool_use", "name": "wan_video", "input": {"prompt": "x" * 5000}},
        ]},
    })
    assert fuori == [
        {"type": "text", "text": "Genero la scena."},
        {"type": "log", "text": "→ wan_video"},
    ]

    assert gateway.traduci({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "y" * 9000}]},
    }) == []


def test_uno_strumento_fallito_si_dice():
    """Senza, la risposta successiva dell'agente sembra arbitraria: ha cambiato
    strada e nessuno sa perche'."""
    fuori = gateway.traduci({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "FAL_KEY mancante"},
        ]},
    })
    assert len(fuori) == 1
    assert "FAL_KEY mancante" in fuori[0]["text"]


def test_il_costo_del_turno_si_vede_sempre():
    """E' un agente che chiama modelli a pagamento: un numero che nessuno vede non
    lo controlla nessuno."""
    fuori = gateway.traduci({
        "type": "result", "subtype": "success", "result": "fatto",
        "duration_ms": 42000, "num_turns": 6, "total_cost_usd": 0.1234,
    })
    assert len(fuori) == 1
    assert "$0.1234" in fuori[0]["text"]
    assert "42s" in fuori[0]["text"]


def test_il_testo_finale_non_si_ripete():
    """`result` riporta l'ultima cosa detta dall'agente, che e' gia' passata come
    `assistant`. Mandarla di nuovo farebbe leggere due volte la stessa risposta."""
    fuori = gateway.traduci({
        "type": "result", "subtype": "success", "result": "Faccio tre scene.",
        "duration_ms": 1000,
    })
    assert all(m["type"] != "text" for m in fuori)


def test_un_turno_finito_male_lo_dice():
    fuori = gateway.traduci({
        "type": "result", "subtype": "error_max_turns", "is_error": True,
        "result": "troppi passaggi", "duration_ms": 900000,
    })
    assert any("error_max_turns" in m["text"] for m in fuori)


def test_la_sessione_si_legge_dall_evento_di_avvio():
    evento = {"type": "system", "subtype": "init", "session_id": "sess-7",
              "model": "claude-opus-5", "tools": ["Read", "Bash"]}
    assert gateway.id_sessione(evento) == "sess-7"
    testo = gateway.traduci(evento)[0]["text"]
    assert "claude-opus-5" in testo and "2 strumenti" in testo


@pytest.mark.parametrize("evento", [
    {"type": "stream_event", "event": {"type": "content_block_delta"}},
    {"type": "assistant", "message": {}},
    {"type": "assistant"},
    {"type": "qualcosa-di-nuovo"},
    {},
])
def test_un_evento_che_non_conosciamo_non_rompe_il_turno(evento):
    """Il formato e' gia' cambiato una volta e cambiera' ancora. Perdere un turno
    gia' pagato per un campo rinominato e' un prezzo troppo alto per la fedelta' a
    uno schema."""
    assert gateway.traduci(evento) == []


# ------------------------------------------------------------------- avvisi


def test_senza_flag_si_avvisa_che_l_agente_non_potra_agire(monkeypatch):
    """Da un websocket nessuno puo' rispondere a una richiesta di permesso:
    l'agente si ferma al primo strumento che ne chiede uno, e da fuori sembra
    rotto invece che prudente."""
    monkeypatch.setattr(gateway, "CLAUDE_FLAGS", [])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-prova")
    avvisi = list(gateway.avvisi_di_configurazione())
    assert any("OM_CLAUDE_FLAGS" in a for a in avvisi)

    monkeypatch.setattr(gateway, "CLAUDE_FLAGS", ["--dangerously-skip-permissions"])
    assert not any("OM_CLAUDE_FLAGS" in a for a in list(gateway.avvisi_di_configurazione()))


def test_senza_chiave_si_dice_subito(monkeypatch):
    monkeypatch.setattr(gateway, "CLAUDE_FLAGS", ["--dangerously-skip-permissions"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert any("ANTHROPIC_API_KEY" in a for a in gateway.avvisi_di_configurazione())


# ------------------------------------------------------------------ websocket


def _client(modulo, tmp_path, monkeypatch, regia=None):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(modulo, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(modulo, "OPENMONTAGE_DIR", tmp_path / "piattaforma")
    monkeypatch.setattr(modulo, "FACTORY_DIR", tmp_path / "factory")
    (tmp_path / "piattaforma").mkdir(exist_ok=True)
    (tmp_path / "factory").mkdir(exist_ok=True)
    return TestClient(modulo.crea_app(regia=regia))


def test_un_progetto_non_ammesso_viene_chiuso_col_motivo(tmp_path, monkeypatch):
    """Rifiutare prima dell'handshake arriva al pannello come «disconnesso», senza
    il perche': si accetta, si dice cos'e' successo, e poi si chiude."""
    from starlette.websockets import WebSocketDisconnect

    client = _client(gateway, tmp_path, monkeypatch)
    with pytest.raises(WebSocketDisconnect) as caduta:
        with client.websocket_connect("/ws?project=../fuga") as ws:
            messaggio = json.loads(ws.receive_text())
            assert "non ammesso" in messaggio["text"]
            ws.receive_text()
    assert caduta.value.code == gateway.CLOSE_BAD_PROJECT


def test_un_messaggio_fa_partire_un_turno_nella_cartella_giusta(tmp_path, monkeypatch):
    class RegiaFinta:
        def __init__(self):
            self.visti = []

        def libero(self):
            return True

        async def esegui(self, workspace, testo, manda):
            self.visti.append((workspace, testo))
            await manda({"type": "text", "text": "ricevuto: " + testo})

    regia = RegiaFinta()
    client = _client(gateway, tmp_path, monkeypatch, regia=regia)
    with client.websocket_connect("/ws?project=omai__pilota") as ws:
        while json.loads(ws.receive_text()).get("type") != "status":
            pass
        ws.send_text(json.dumps({"type": "message", "text": "costruisci un trailer"}))
        assert json.loads(ws.receive_text()) == {"type": "text", "text": "ricevuto: costruisci un trailer"}

    assert regia.visti == [(tmp_path / "omai__pilota", "costruisci un trailer")]
    assert (tmp_path / "omai__pilota" / "CLAUDE.md").exists()


def test_un_messaggio_vuoto_non_fa_partire_niente(tmp_path, monkeypatch):
    """Un turno costa: un invio a vuoto non deve diventare una chiamata."""
    class RegiaFinta:
        def __init__(self):
            self.turni = 0

        def libero(self):
            return True

        async def esegui(self, workspace, testo, manda):
            self.turni += 1
            await manda({"type": "text", "text": testo})

    regia = RegiaFinta()
    client = _client(gateway, tmp_path, monkeypatch, regia=regia)
    with client.websocket_connect("/ws?project=omai__pilota") as ws:
        while json.loads(ws.receive_text()).get("type") != "status":
            pass
        ws.send_text(json.dumps({"type": "message", "text": "   "}))
        ws.send_text(json.dumps({"type": "ping"}))
        ws.send_text(json.dumps({"type": "message", "text": "vero"}))
        # I due scartati non rispondono: si aspetta la risposta del terzo, che
        # arriva per forza dopo, e a quel punto il conto e' definitivo.
        assert json.loads(ws.receive_text()) == {"type": "text", "text": "vero"}

    assert regia.turni == 1


# --------------------------------------------------------------------- turno


def _finto_claude(tmp_path: Path, corpo: str) -> Path:
    binario = tmp_path / "finto-claude"
    binario.write_text("#!/usr/bin/env python3\n" + corpo, encoding="utf-8")
    binario.chmod(0o755)
    return binario


async def _raccogli(regia, workspace, testo):
    ricevuti = []

    async def manda(messaggio):
        ricevuti.append(messaggio)

    await regia.esegui(workspace, testo, manda)
    return ricevuti


@pytest.mark.asyncio
async def test_un_turno_che_non_risponde_viene_fermato(tmp_path, monkeypatch):
    """Un render appeso tiene Chromium e un giga di memoria: su questa macchina e'
    il pannello che smette di rispondere. Il turno parte in un gruppo di processi
    suo apposta perche' al timeout se ne vada l'albero intero."""
    binario = _finto_claude(tmp_path, "import time\ntime.sleep(120)\n")
    monkeypatch.setattr(gateway, "CLAUDE_BIN", str(binario))
    monkeypatch.setattr(gateway, "CLAUDE_FLAGS", [])
    workspace = tmp_path / "omai__pilota"
    workspace.mkdir()

    ricevuti = await _raccogli(gateway.Regia(timeout_s=1), workspace, "ciao")

    assert any("fermo il turno" in m.get("text", "") for m in ricevuti)
    # Lo stato torna «pronto» comunque: un turno ucciso non deve lasciare la chat
    # a credere che stia ancora lavorando.
    assert ricevuti[-1] == {"type": "status", "status": "pronto"}


@pytest.mark.asyncio
async def test_se_la_cli_muore_si_dice_perche(tmp_path, monkeypatch):
    """Senza questo, un turno che muore per una chiave mancante lascia la chat muta
    e sembra che l'agente non abbia niente da dire."""
    binario = _finto_claude(tmp_path, "import sys\nsys.stderr.write('ANTHROPIC_API_KEY mancante\\n')\nsys.exit(2)\n")
    monkeypatch.setattr(gateway, "CLAUDE_BIN", str(binario))
    monkeypatch.setattr(gateway, "CLAUDE_FLAGS", [])
    workspace = tmp_path / "omai__pilota"
    workspace.mkdir()

    ricevuti = await _raccogli(gateway.Regia(timeout_s=20), workspace, "ciao")

    assert any("uscito con 2" in m.get("text", "") for m in ricevuti)
    assert any("ANTHROPIC_API_KEY mancante" in m.get("text", "") for m in ricevuti)


@pytest.mark.asyncio
async def test_una_riga_che_non_e_json_si_mostra_invece_di_sparire(tmp_path, monkeypatch):
    binario = _finto_claude(tmp_path, "print('Error: model not available')\n")
    monkeypatch.setattr(gateway, "CLAUDE_BIN", str(binario))
    monkeypatch.setattr(gateway, "CLAUDE_FLAGS", [])
    workspace = tmp_path / "omai__pilota"
    workspace.mkdir()

    ricevuti = await _raccogli(gateway.Regia(timeout_s=20), workspace, "ciao")

    assert any("model not available" in m.get("text", "") for m in ricevuti)


@pytest.mark.asyncio
async def test_il_secondo_turno_aspetta_il_primo(tmp_path, monkeypatch):
    """Due render in parallelo aprono due Chromium, e su un N100 con 16 GB si
    portano via il pannello. Chi arriva secondo deve aspettare, e deve saperlo."""
    binario = _finto_claude(tmp_path, "import time\ntime.sleep(0.6)\n")
    monkeypatch.setattr(gateway, "CLAUDE_BIN", str(binario))
    monkeypatch.setattr(gateway, "CLAUDE_FLAGS", [])
    (tmp_path / "uno").mkdir()
    (tmp_path / "due").mkdir()
    regia = gateway.Regia(max_concurrent=1, timeout_s=20)

    primo, secondo = await asyncio.gather(
        _raccogli(regia, tmp_path / "uno", "a"),
        _raccogli(regia, tmp_path / "due", "b"),
    )

    # Uno dei due ha dovuto aspettare, e gli e' stato detto.
    attese = [m for m in primo + secondo if "in coda" in str(m.get("status", ""))]
    assert len(attese) == 1
