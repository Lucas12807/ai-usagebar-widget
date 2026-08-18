"""
Busca e normaliza os dados do `ai-usagebar usage --json`.

Motor GENÉRICO: não há nenhum vendor hardcoded (nem "Claude", nem
"DeepSeek"). O widget varre TODAS as entries que o `ai-usagebar` devolver
com status "ready" e monta um bloco pra cada métrica encontrada — funciona
pra qualquer um dos provedores que o `ai-usagebar` já sabe consultar
(Claude/Anthropic, OpenAI/Codex, Z.AI, OpenRouter, DeepSeek, Kimi,
Moonshot, Grok/xAI, Google Antigravity, Cursor, MiniMax, Kiro, ...). Se a
pessoa só configurar o Claude, só o bloco do Claude aparece; se configurar
5 provedores, aparecem 5 (ou mais, um bloco por métrica). Habilitar um
provedor novo é mexer só no `config.toml` do `ai-usagebar` — nunca neste
arquivo.

Formato esperado de `ai-usagebar usage --json`:

    {"entries": [
        {"id": "anthropic", "display_name": "Claude", "status": "ready",
         "sections": [
             {"type": "metric", "label": "Session (5h)", "percent": 38,
              "value": "38%", "detail": "Resets in 2h 37m · ...",
              "reset_at": "2026-08-18T16:40:00Z"},
             {"type": "metric", "label": "Weekly (7d)", "percent": 48, ...}
         ]},
        {"id": "deepseek", "display_name": "DeepSeek", "status": "ready",
         "sections": [
             {"type": "text", "label": "Balance", "value": "$8.84"}, ...
         ]},
        {"id": "zai", "display_name": "Z.AI", "status": "error",
         "error": "... no API key. Either set ..."},
        ...
    ], "primary": null}

Dois formatos de item dentro de `sections`:
  - `type: "metric"` (ex. Claude): já vem com `percent` numérico e
    `reset_at` ISO — usado direto, sem parsing de texto.
  - `type: "text"` (ex. DeepSeek): só um `value` em texto livre (ex.
    "$8.84"), sem percent — mostrado como está.
Provedores novos podem usar um desses dois formatos ou algo parecido; o
parser tenta ambos antes de desistir de um item.

Vendors que a pessoa nunca configurou (a maioria, por padrão) aparecem com
status "error" e mensagens tipo "no API key" / arquivo não encontrado —
isso é tratado como "não configurado", não como erro real, e o vendor
simplesmente não gera bloco nenhum. Só entra na lista de erros (visível no
menu "Ver erro" do widget) quando a mensagem não bate com esse padrão —
ou seja, parece um vendor que a pessoa configurou de propósito mas está
com problema de verdade (token expirado, rede, etc).
"""

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

# Nome do binário. Se você não colocou no PATH, troque pelo caminho completo,
# ex: r"C:\Users\SeuUsuario\.cargo\bin\ai-usagebar.exe"
AI_USAGEBAR_BIN = "ai-usagebar"

# Quantos segundos entre uma consulta e outra.
POLL_INTERVAL_SECONDS = 60

MOCK_BLOCKS = [
    {"label": "Claude · Session (5h)", "value_text": "42% · 2h 13m", "percent": 42},
    {"label": "Claude · Weekly (7d)", "value_text": "18% · 3d 4h", "percent": 18},
    {"label": "DeepSeek · Balance", "value_text": "$12.34", "percent": None},
]

# Trechos de mensagem de erro que indicam "vendor nunca configurado" (não é
# um erro de verdade, é só um provedor que a pessoa não usa). Case-insensitive.
_UNCONFIGURED_HINTS = [
    "no api key",
    "não pode encontrar",
    "cannot find",
    "not found",
    "no such file",
    "does not exist",
    "não encontrado",
]


def fetch_raw_json():
    """Roda `ai-usagebar usage --json` e devolve o dict do JSON."""
    exe = shutil.which(AI_USAGEBAR_BIN) or AI_USAGEBAR_BIN
    result = subprocess.run(
        [exe, "usage", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",  # o ai-usagebar sempre imprime UTF-8; sem isso o
        errors="replace",  # Windows decodifica na codepage do console e corrompe acentos
        timeout=15,
        # Evita a janela de console que o Windows abriria ao rodar o
        # subprocess, mesmo com o widget executando via pythonw.exe.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # `usage` sai com exit code != 0 se QUALQUER vendor configurado falhar
    # (ex.: token expirado, chave faltando) — mas ainda imprime o envelope
    # JSON completo com os vendors que funcionaram. Por isso tentamos
    # json.loads antes de desistir, em vez de confiar só no returncode.
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"ai-usagebar saiu com erro: {result.stderr.strip() or result.stdout.strip()}")


def _seconds_until(iso_timestamp):
    """'2026-08-18T16:40:00Z' -> segundos a partir de agora (UTC). None se
    o texto não vier ou não for parseável."""
    if not iso_timestamp:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_timestamp).replace("Z", "+00:00"))
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(delta))
    except (ValueError, TypeError):
        return None


def _extract_reset_seconds(text):
    """Tenta achar algo tipo '2h 13m' / '3d 4h' / '45m' num texto e
    converter pra segundos. Retorna None se não achar nada reconhecível."""
    if not text:
        return None
    days = re.search(r"(\d+)\s*d", text)
    hours = re.search(r"(\d+)\s*h", text)
    minutes = re.search(r"(\d+)\s*m(?!s)", text)
    if not (days or hours or minutes):
        return None
    total = 0
    if days:
        total += int(days.group(1)) * 86400
    if hours:
        total += int(hours.group(1)) * 3600
    if minutes:
        total += int(minutes.group(1)) * 60
    return total


def _looks_unconfigured(error_msg):
    if not error_msg:
        return False
    msg = error_msg.lower()
    return any(hint in msg for hint in _UNCONFIGURED_HINTS)


def _blocks_from_entry(entry):
    """Monta 0+ blocos genéricos a partir de UMA entry pronta (status=ready).
    Cada item de `sections` que tiver um `label` reconhecível vira um bloco."""
    display_name = entry.get("display_name") or entry.get("id") or "?"
    blocks = []
    for item in entry.get("sections") or []:
        label = item.get("label")
        if not label:
            continue  # spacers e afins não têm label

        percent = item.get("percent")
        if percent is not None and not isinstance(percent, (int, float)):
            # Ignora percent que não seja numérico — protege o cálculo de
            # cor no widget.py contra um vendor mal comportado.
            percent = None
        if percent is not None:
            # Formato "metric" (visto no Claude): percent numérico + reset_at ISO.
            reset_seconds = _seconds_until(item.get("reset_at"))
            if reset_seconds is None:
                reset_seconds = _extract_reset_seconds(item.get("detail"))
            value_text = f"{percent}% · {format_seconds(reset_seconds)}" if reset_seconds is not None else f"{percent}%"
        elif item.get("value") is not None:
            # Formato "text" (visto no DeepSeek): só um texto livre já pronto.
            value_text = str(item["value"])
        else:
            continue  # item sem percent nem value (ex.: "block" com body) — ignora

        blocks.append({
            "label": f"{display_name} · {label}",
            "value_text": value_text,
            "percent": percent,
        })
    return blocks


def extract_blocks(raw):
    """Varre entries[] inteiro e devolve (blocks, errors). `blocks` é uma
    lista genérica pronta pra desenhar; `errors` só contém vendors que
    parecem configurados de propósito mas estão falhando de verdade."""
    blocks = []
    errors = []
    for entry in raw.get("entries", []) or []:
        display_name = entry.get("display_name") or entry.get("id") or "?"
        if entry.get("status") == "ready":
            blocks.extend(_blocks_from_entry(entry))
        else:
            msg = entry.get("error") or entry.get("status") or "erro desconhecido"
            if not _looks_unconfigured(msg):
                errors.append(f"{display_name}: {msg}")
    return blocks, errors


def get_usage(mock=False):
    if mock:
        blocks = [dict(b) for b in MOCK_BLOCKS]
        # simula variação ao longo do tempo pra ver a UI reagir
        blocks[0]["percent"] = int(30 + 20 * (time.time() % 60) / 60)
        blocks[0]["value_text"] = f"{blocks[0]['percent']}% · 2h 13m"
        return {"blocks": blocks, "error": None}
    try:
        raw = fetch_raw_json()
        blocks, errors = extract_blocks(raw)
        return {"blocks": blocks, "error": " | ".join(errors) if errors else None}
    except FileNotFoundError:
        return {"blocks": [], "error": "ai-usagebar não encontrado no PATH."}
    except Exception as exc:  # noqa: BLE001 - queremos mostrar qualquer erro na UI
        return {"blocks": [], "error": f"Falha ao consultar: {exc}"}


def format_seconds(seconds):
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
