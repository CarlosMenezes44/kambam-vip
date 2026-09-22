import argparse
import base64
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://agilitytelecomudp.com.br/webservice/v1"
TOKEN_PATHS = [
    ROOT / ".openclaw" / "secrets" / "ixc_api_token.txt",
    ROOT.parents[1] / ".openclaw" / "secrets" / "ixc_api_token.txt",
]
SNAPSHOT_DIR = ROOT / "relatorios" / "os_monitor"
TZ = ZoneInfo("America/Sao_Paulo")

TARGET_FILIAIS = {
    "1": "Uniao",
    "3": "Queimadas",
}
TECHNICAL_SETOR_ID = "1"
TURN_LABELS = {
    "M": "manha",
    "T": "tarde",
    "N": "noite",
    "Q": "qualquer/sem turno fixo",
}
DESCRIPTION_AGENDA_TERMS = [
    "agenda",
    "agendado",
    "manha",
    "tarde",
    "noite",
    "apos",
    "antes",
    "a partir",
    "disponivel",
    "ligar",
    "sabado",
    "domingo",
    "amanha",
    "folga",
]
TIME_PATTERN = re.compile(r"\b\d{1,2}\s*(?::|h|hrs|horas)\s*\d{0,2}\b", re.IGNORECASE)
WEEKDAY_NAMES = {
    "segunda": 0,
    "terca": 1,
    "terça": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["morning", "midday"], required=True)
    parser.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--cutoff", default="13:00:00", help="HH:MM:SS for midday")
    return parser.parse_args()


def read_token():
    env_token = os.environ.get("IXC_API_TOKEN", "").strip()
    if env_token:
        return env_token

    checked = []
    for token_path in TOKEN_PATHS:
        checked.append(str(token_path))
        if not token_path.exists():
            continue
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"Token vazio em {token_path}")
        return token
    raise FileNotFoundError("Token IXC nao encontrado. Caminhos verificados: " + ", ".join(checked))


def headers():
    auth = base64.b64encode(read_token().encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {auth}", "ixcsoft": "listar"}


def listar(table, qtype, query, oper="=", page=1, rp=500, sortname=None, sortorder="asc"):
    body = {
        "qtype": qtype,
        "query": str(query),
        "oper": oper,
        "page": str(page),
        "rp": str(rp),
        "sortname": sortname or qtype,
        "sortorder": sortorder,
    }
    response = requests.post(
        f"{BASE_URL}/{table}", headers=headers(), json=body, timeout=45
    )
    response.raise_for_status()
    return response.json()


def listar_tudo(table, qtype, query, oper="=", sortname=None, sortorder="asc", rp=500):
    rows = []
    page = 1
    while True:
        data = listar(table, qtype, query, oper, page, rp, sortname, sortorder)
        regs = data.get("registros") or []
        rows.extend(regs)
        total = int(data.get("total") or 0)
        if len(rows) >= total or not regs:
            return rows
        page += 1


def map_table(table, value_field):
    rows = listar_tudo(table, f"{table}.id", "0", ">", f"{table}.id", "asc", rp=1000)
    return {str(row.get("id")): row.get(value_field) for row in rows}


def fetch_client_names(client_ids):
    names = {}
    for client_id in sorted({str(value) for value in client_ids if value}):
        data = listar(
            "cliente",
            "cliente.id",
            client_id,
            "=",
            rp=1,
            sortname="cliente.id",
        )
        regs = data.get("registros") or []
        if regs:
            names[client_id] = regs[0].get("razao") or regs[0].get("fantasia") or f"ID {client_id}"
    return names


def parse_dt(value):
    if not value or str(value).startswith("0000"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def target_name(row):
    filial = str(row.get("id_filial") or "")
    return TARGET_FILIAIS.get(filial)


def fmt_percent(value):
    return f"{value:.1f}".replace(".", ",")


def normalize_text(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in value if not unicodedata.combining(ch)).lower()


def mask_sensitive_text(value):
    value = str(value or "")
    value = re.sub(r"\b\d{10,13}\b", "[telefone]", value)
    value = re.sub(r"\b\d{3}[.\s-]?\d{3}[.\s-]?\d{3}[.\s-]?\d{2}\b", "[documento]", value)
    return value


def description_agenda_text(row):
    raw = str(row.get("mensagem") or "")
    if not raw.strip() and row.get("agenda_descricao"):
        return str(row.get("agenda_descricao") or "")
    if not raw.strip():
        return "sem descricao"

    text = raw.replace("\r", " ").replace("\n", " ")
    chunks = [chunk.strip() for chunk in re.split(r"(?<=[.!?])\s+|\s{2,}", text) if chunk.strip()]
    matches = []
    for chunk in chunks:
        normalized = normalize_text(chunk)
        has_term = any(term in normalized for term in DESCRIPTION_AGENDA_TERMS)
        if has_term or TIME_PATTERN.search(chunk):
            cleaned = mask_sensitive_text(chunk)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            matches.append(cleaned[:180])
        if len(matches) >= 2:
            break

    if not matches:
        return "sem agenda na descricao"
    return " | ".join(matches)


def has_description_agenda(row):
    return description_agenda_text(row) not in {
        "sem descricao",
        "sem agenda na descricao",
    }


def extract_minutes_from_description(row):
    text = normalize_text(row.get("mensagem") or row.get("agenda_descricao"))
    matches = re.findall(r"\b(\d{1,2})\s*(?::|h|hrs|horas)\s*(\d{0,2})\b", text, re.IGNORECASE)
    minutes = []
    for hour, minute in matches:
        hour = int(hour)
        minute = int(minute or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            minutes.append(hour * 60 + minute)
    return minutes


def completed_by_cutoff(row, cutoff_dt):
    fechamento = parse_dt(row.get("data_fechamento"))
    return row.get("status") == "F" and fechamento and fechamento <= cutoff_dt


def started_by_cutoff(row, day_start, cutoff_dt):
    started = parse_dt(row.get("data_inicio")) or parse_dt(row.get("data_hora_execucao"))
    if started and day_start <= started <= cutoff_dt:
        return True
    return row.get("status") in {"EX", "EP"}


def operation_flow(rows, finished, open_rows, day_start, cutoff_dt):
    started_rows = [row for row in rows if started_by_cutoff(row, day_start, cutoff_dt)]
    started_ids = {str(row.get("id")) for row in started_rows}
    finished_ids = {str(row.get("id")) for row in finished}
    started_not_finished = [
        row for row in open_rows if str(row.get("id")) in started_ids and str(row.get("id")) not in finished_ids
    ]
    not_started = [row for row in open_rows if str(row.get("id")) not in started_ids]
    total = len(rows)
    return {
        "total": total,
        "started": len(started_rows),
        "finished": len(finished),
        "started_not_finished": len(started_not_finished),
        "not_started": len(not_started),
        "pending": len(open_rows),
        "started_pct": round(len(started_rows) / total * 100, 1) if total else 0,
        "finished_pct": round(len(finished) / total * 100, 1) if total else 0,
        "started_not_finished_ids": [str(row.get("id")) for row in started_not_finished],
        "not_started_ids": [str(row.get("id")) for row in not_started],
    }


def description_target_date(row):
    text = normalize_text(row.get("mensagem") or row.get("agenda_descricao"))
    abertura = parse_dt(row.get("data_abertura"))
    if not abertura:
        return None
    base_date = abertura.date()
    if "amanha" in text:
        return base_date + timedelta(days=1)
    for name, weekday in WEEKDAY_NAMES.items():
        if name in text:
            days = (weekday - base_date.weekday()) % 7
            return base_date + timedelta(days=days)
    return None


def description_due_status(row, cutoff_dt):
    if not has_description_agenda(row):
        return None
    if completed_by_cutoff(row, cutoff_dt):
        return "cumprido"

    text = normalize_text(row.get("mensagem"))
    target_date = description_target_date(row)
    if target_date and cutoff_dt.date() < target_date:
        return f"aguardando data descrita ({target_date.strftime('%d/%m')})"

    cutoff_minutes = cutoff_dt.hour * 60 + cutoff_dt.minute
    times = extract_minutes_from_description(row)

    due = False
    reason = ""
    if "manha" in text and cutoff_minutes >= 13 * 60:
        due = True
        reason = "turno da manha ja passou"
    elif "tarde" in text and cutoff_minutes >= 19 * 60:
        due = True
        reason = "turno da tarde ja passou"
    elif times:
        if any(word in text for word in ["entre", "ate", "até"]):
            due_minute = max(times)
            due = cutoff_minutes >= due_minute
            reason = f"janela descrita ate {due_minute // 60:02d}:{due_minute % 60:02d}"
        else:
            first = min(times)
            due_minute = first + 180
            due = cutoff_minutes >= due_minute
            reason = f"disponibilidade desde {first // 60:02d}:{first % 60:02d}"

    if due:
        return f"nao cumprido - {reason}"
    return "aguardando janela"


def fetch_agenda(day):
    rows = listar_tudo(
        "su_oss_chamado",
        "su_oss_chamado.data_agenda",
        day,
        ">=",
        "su_oss_chamado.data_agenda",
        "asc",
        rp=500,
    )
    scheduled = []
    for row in rows:
        agenda = parse_dt(row.get("data_agenda"))
        if not agenda or agenda.date().isoformat() != day:
            continue
        if not target_name(row):
            continue
        if str(row.get("setor") or "") != TECHNICAL_SETOR_ID:
            continue
        if str(row.get("id_tecnico") or "0") == "0":
            continue
        fechamento = parse_dt(row.get("data_fechamento"))
        if str(row.get("status") or "").upper() == "F" and fechamento and fechamento.date().isoformat() != day:
            continue
        scheduled.append(row)
    return scheduled


def fetch_by_ids(ids):
    rows = []
    for os_id in ids:
        data = listar(
            "su_oss_chamado",
            "su_oss_chamado.id",
            os_id,
            "=",
            rp=1,
            sortname="su_oss_chamado.id",
        )
        regs = data.get("registros") or []
        if regs:
            rows.append(regs[0])
    return rows


def snapshot_path(day):
    return SNAPSHOT_DIR / f"snapshot_os_{day}.json"


def preserve_existing_snapshot(path):
    if not path.exists():
        return None
    stamp = datetime.now(TZ).strftime("%H%M%S")
    backup = path.with_name(f"{path.stem}_backup_{stamp}{path.suffix}")
    shutil.copy2(path, backup)
    return backup


def has_marked_time(row):
    agenda = parse_dt(row.get("data_agenda"))
    return bool(agenda and agenda.time() != time.min)


def turn_code(row):
    return str(row.get("melhor_horario_agenda") or "").strip().upper()


def has_fixed_turn(row):
    return turn_code(row) in {"M", "T", "N"}


def schedule_text(row):
    start = parse_dt(row.get("data_agenda"))
    end = parse_dt(row.get("data_agenda_final"))
    if start and end:
        if start.date() == end.date():
            agenda = f"{start.strftime('%H:%M')} a {end.strftime('%H:%M')}"
        else:
            agenda = f"{start.strftime('%d/%m %H:%M')} a {end.strftime('%d/%m %H:%M')}"
    elif start:
        agenda = start.strftime("%H:%M")
    else:
        agenda = "sem horario"
    code = turn_code(row)
    label = TURN_LABELS.get(code, "nao informado")
    if code:
        return f"{agenda} | turno: {code} ({label})"
    return f"{agenda} | turno: nao informado"


def description_schedule_label(row):
    category = description_info_category(row)
    times = sorted(set(extract_minutes_from_description(row)))
    if category == "Sem agenda desc":
        return "desc: sem turno/horario"
    if category == "Intervalo":
        return f"desc: intervalo {fmt_minutes(times[0])}-{fmt_minutes(times[-1])}"
    if category == "Horario":
        return f"desc: horario {fmt_minutes(times[0])}"
    if category in {"Manha", "Tarde", "Noite"}:
        return f"desc: turno {category.lower()}"
    target_date = description_target_date(row)
    if target_date:
        return f"desc: data {target_date.strftime('%d/%m')}"
    return "desc: info generica de agenda"


def description_schedule_details(row):
    text = normalize_text(row.get("mensagem") or row.get("agenda_descricao") or "")
    turns = []
    if "manha" in text:
        turns.append("manha")
    if "tarde" in text:
        turns.append("tarde")
    if "noite" in text:
        turns.append("noite")
    has_agenda_context = bool(turns) or any(term in text for term in DESCRIPTION_AGENDA_TERMS)
    times = sorted(set(extract_minutes_from_description(row))) if has_agenda_context else []
    target_date = description_target_date(row)
    return {
        "tem_turno_na_descricao": bool(turns),
        "turnos_na_descricao": turns,
        "tem_horario_na_descricao": bool(times),
        "horarios_na_descricao": [fmt_minutes(value) for value in times],
        "tem_intervalo_na_descricao": len(times) >= 2,
        "intervalo_na_descricao": f"{fmt_minutes(times[0])}-{fmt_minutes(times[-1])}"
        if len(times) >= 2
        else "",
        "data_mencionada_na_descricao": target_date.strftime("%Y-%m-%d") if target_date else "",
    }


def description_schedule_pipe(row):
    details = description_schedule_details(row)
    turn = ",".join(details["turnos_na_descricao"]) if details["turnos_na_descricao"] else "--"
    times = ",".join(details["horarios_na_descricao"]) if details["horarios_na_descricao"] else "--"
    interval = details["intervalo_na_descricao"] or "--"
    date_text = details["data_mencionada_na_descricao"] or "--"
    return f"AGENDA DESC: turno={turn} | horario={times} | intervalo={interval} | data={date_text}"


def clean_schedule_text(row):
    details = description_schedule_details(row)
    parts = []
    if details["turnos_na_descricao"]:
        parts.append("turno " + ",".join(details["turnos_na_descricao"]))
    if details["intervalo_na_descricao"]:
        parts.append("intervalo " + details["intervalo_na_descricao"])
    elif details["horarios_na_descricao"]:
        parts.append("horario " + ",".join(details["horarios_na_descricao"]))
    if details["data_mencionada_na_descricao"]:
        parts.append("data " + details["data_mencionada_na_descricao"])
    return " / ".join(parts) if parts else "sem turno/horario na descricao"


def clean_result_text(compliance):
    status = (compliance or {}).get("status") or "sem analise"
    if status == "cumprido":
        return "cumpriu"
    if status == "nao cumprido":
        return "nao cumpriu"
    if status in {"aguardando janela", "aguardando data descrita"}:
        return "aguardando"
    if status == "sem agendamento na descricao":
        return "sem agenda clara"
    return status


def schedule_compliance(row, reference_dt):
    fechamento = parse_dt(row.get("data_fechamento"))
    due_from_description = description_due_status(row, reference_dt) if reference_dt else None
    target_date = description_target_date(row)
    details = description_schedule_details(row)

    if not (
        details["tem_turno_na_descricao"]
        or details["tem_horario_na_descricao"]
        or details["data_mencionada_na_descricao"]
    ):
        return {
            "emoji": "🟡",
            "status": "sem agendamento na descricao",
            "motivo": "sem turno, horario ou data clara na descricao",
            "nivel": "informativo",
        }

    if row.get("status") == "F" and fechamento and (not reference_dt or fechamento <= reference_dt):
        return {
            "emoji": "✅",
            "status": "cumprido",
            "motivo": f"finalizada as {fechamento.strftime('%H:%M')}",
            "nivel": "ok",
        }

    if due_from_description and due_from_description.startswith("nao cumprido"):
        return {
            "emoji": "❌",
            "status": "nao cumprido",
            "motivo": due_from_description,
            "nivel": "critico",
        }

    if target_date and reference_dt and reference_dt.date() > target_date:
        return {
            "emoji": "❌",
            "status": "nao cumprido",
            "motivo": f"data descrita passou em {target_date.strftime('%d/%m')}",
            "nivel": "critico",
        }

    if target_date and reference_dt and reference_dt.date() < target_date:
        return {
            "emoji": "🕒",
            "status": "aguardando data descrita",
            "motivo": f"descricao aponta {target_date.strftime('%d/%m')}",
            "nivel": "ok",
        }

    return {
        "emoji": "🕒",
        "status": "aguardando janela",
        "motivo": due_from_description or "agenda ainda dentro da janela",
        "nivel": "ok",
    }


def compact_row(row, funcionarios, assuntos, clientes, reference_dt=None):
    client_id = str(row.get("id_cliente") or "")
    compliance = schedule_compliance(row, reference_dt) if reference_dt else None
    return {
        "id": str(row.get("id") or ""),
        "id_cliente": client_id,
        "cliente": clientes.get(client_id, f"ID {client_id}") if client_id else "",
        "filial": str(row.get("id_filial") or ""),
        "filial_nome": target_name(row),
        "status": row.get("status"),
        "id_tecnico": str(row.get("id_tecnico") or "0"),
        "tecnico": funcionarios.get(str(row.get("id_tecnico") or ""), f"ID {row.get('id_tecnico')}"),
        "id_assunto": str(row.get("id_assunto") or ""),
        "assunto": assuntos.get(str(row.get("id_assunto") or ""), str(row.get("id_assunto") or "")),
        "data_abertura": row.get("data_abertura") or "",
        "data_agenda": row.get("data_agenda") or "",
        "data_agenda_final": row.get("data_agenda_final") or "",
        "melhor_horario_agenda": row.get("melhor_horario_agenda") or "",
        "agenda_descricao": description_agenda_text(row),
        "data_inicio": row.get("data_inicio") or "",
        "data_hora_execucao": row.get("data_hora_execucao") or "",
        "data_fechamento": row.get("data_fechamento") or "",
        "status_sla": row.get("status_sla") or "",
        "data_prazo_limite": row.get("data_prazo_limite") or "",
        "agendamento_descricao": description_schedule_details(row),
        "cumprimento_agenda": compliance,
    }


def status_counts(rows):
    return Counter(row.get("status") for row in rows)


def by_filial(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[target_name(row) or "Sem filial"].append(row)
    return grouped


def by_technician(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(row.get("id_tecnico") or "0")].append(row)
    return grouped


def format_status(counter):
    if not counter:
        return "-"
    return ", ".join(f"{status or 'vazio'}={count}" for status, count in sorted(counter.items()))


def format_subject_summary(subjects, limit=3):
    items = list(subjects.items())[:limit]
    text = ", ".join(f"{subject}={count}" for subject, count in items)
    extra = len(subjects) - len(items)
    if extra > 0:
        text += f", +{extra} assuntos"
    return text or "-"


def format_interval_summary(item):
    if item.get("intervalo_medio_min") is None:
        return "intervalo: sem dados suficientes"
    return (
        f"intervalo medio: {item['intervalo_medio_min']} min"
        f" | menor: {item['menor_intervalo_min']} min"
        f" | maior: {item['maior_intervalo_min']} min"
    )


def sla_attention(rows, reference_dt, funcionarios, assuntos, limit=12):
    items = []
    for row in rows:
        if row.get("status") == "F":
            continue
        abertura = parse_dt(row.get("data_abertura"))
        agenda = parse_dt(row.get("data_agenda"))
        agenda_final = parse_dt(row.get("data_agenda_final"))
        inicio = parse_dt(row.get("data_inicio")) or parse_dt(row.get("data_hora_execucao"))
        reasons = []
        severity = 0

        if agenda_final and agenda_final <= reference_dt:
            delay_min = round((reference_dt - agenda_final).total_seconds() / 60)
            reasons.append(f"janela vencida ha {delay_min} min")
            severity += 10000 + max(delay_min, 0)
        elif agenda and agenda <= reference_dt and not inicio:
            delay_min = round((reference_dt - agenda).total_seconds() / 60)
            reasons.append(f"horario passou sem inicio ha {delay_min} min")
            severity += 5000 + max(delay_min, 0)

        if abertura:
            age_hours = (reference_dt - abertura).total_seconds() / 3600
            if age_hours >= 24:
                reasons.append(f"aberta ha {age_hours:.1f}h")
                severity += 1000 + round(age_hours)

        if reasons and row.get("status_sla") == "N":
            reasons.append("status_sla=N")
            severity += 100

        if not reasons:
            continue

        tech_id = str(row.get("id_tecnico") or "0")
        subject_id = str(row.get("id_assunto") or "")
        items.append(
            {
                "severity": severity,
                "id": row.get("id"),
                "filial": target_name(row) or row.get("id_filial"),
                "status": row.get("status"),
                "tecnico": funcionarios.get(tech_id, f"ID {tech_id}"),
                "assunto": assuntos.get(subject_id, subject_id),
                "agenda": row.get("data_agenda") or "",
                "agenda_final": row.get("data_agenda_final") or "",
                "reasons": reasons,
            }
        )

    return sorted(items, key=lambda item: item["severity"], reverse=True)[:limit]


def format_attention(items):
    if not items:
        return ["- Nenhuma OS em atencao pelo criterio atual."]
    lines = []
    for item in items:
        lines.append(
            f"- OS {item['id']} | {item['filial']} | {item['status']} | {item['tecnico']} | {item['assunto']} | {'; '.join(item['reasons'])}"
        )
    return lines


def supervisor_queue(open_rows, cutoff_dt, day_start, funcionarios, assuntos, limit=10):
    attention_by_id = {
        str(item.get("id")): item
        for item in sla_attention(open_rows, cutoff_dt, funcionarios, assuntos, limit=max(len(open_rows), limit))
    }
    items = []
    for row in open_rows:
        row_id = str(row.get("id") or "")
        tech_id = str(row.get("id_tecnico") or "0")
        subject_id = str(row.get("id_assunto") or "")
        started = started_by_cutoff(row, day_start, cutoff_dt)
        fechamento = parse_dt(row.get("data_fechamento"))
        finalized_after_cutoff = row.get("status") == "F" and fechamento and fechamento > cutoff_dt
        due_status = description_due_status(row, cutoff_dt) or ""
        attention = attention_by_id.get(row_id)
        reasons = list(attention.get("reasons", [])) if attention else []
        tags = []
        severity = 0

        if due_status.startswith("nao cumprido"):
            tags.append("agenda_nao_cumprida")
            reasons.insert(0, due_status)
            severity += 9000

        if attention:
            if any("janela vencida" in reason or "horario passou" in reason for reason in reasons):
                tags.append("sla_vencido")
                severity += 7000
            if any("aberta ha" in reason for reason in reasons):
                tags.append("passivo_antigo")
                severity += 3000

        if finalized_after_cutoff:
            tags.append("finalizada_apos_corte")
            reasons.append(f"finalizada depois do corte em {fechamento.strftime('%H:%M')}")
            severity += 2000
        elif started:
            tags.append("iniciada_sem_baixa")
            severity += 5000
        else:
            tags.append("sem_inicio_ixc")
            severity += 4000

        if not reasons:
            reasons.append("pendente no corte")

        if "agenda_nao_cumprida" in tags:
            action = "replanejar_rota_hoje"
        elif "finalizada_apos_corte" in tags:
            action = "medir_baixa_tardia"
        elif "iniciada_sem_baixa" in tags:
            action = "cobrar_baixa_ixc"
        elif "sla_vencido" in tags:
            action = "acionar_responsavel_agora"
        elif "passivo_antigo" in tags:
            action = "validar_passivo_antigo"
        else:
            action = "confirmar_execucao_campo"

        items.append(
            {
                "severity": severity,
                "id": row_id,
                "filial": target_name(row) or row.get("id_filial"),
                "status": row.get("status"),
                "tecnico": funcionarios.get(tech_id, f"ID {tech_id}"),
                "assunto": assuntos.get(subject_id, subject_id),
                "caixa": ",".join(dict.fromkeys(tags)),
                "motivo": "; ".join(dict.fromkeys(reasons))[:240],
                "acao": action,
            }
        )

    return sorted(items, key=lambda item: item["severity"], reverse=True)[:limit]


def format_supervisor_queue(items):
    if not items:
        return ["- Nenhuma OS pendente para fila do supervisor."]
    lines = []
    for item in items:
        lines.append(
            f"- OS {item['id']} | {item['filial']} | {item['status']} | {item['tecnico']} | {item['assunto']} | caixa: {item['caixa']} | acao: {item['acao']} | motivo: {item['motivo']}"
        )
    return lines


def load_maps():
    return map_table("funcionarios", "funcionario"), map_table("su_oss_assunto", "assunto")


def os_sort_key(row):
    return parse_dt(row.get("data_agenda")) or datetime.max.replace(tzinfo=TZ)


def first_name(value):
    parts = str(value or "").strip().split()
    return parts[0].upper() if parts else "-"


def description_agenda_marker(row):
    return ">>> SIM <<<" if has_description_agenda(row) else "NAO"


def fmt_minutes(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def description_info_category(row):
    if not has_description_agenda(row):
        return "Sem agenda desc"
    text = normalize_text(row.get("mensagem"))
    times = sorted(set(extract_minutes_from_description(row)))
    if len(times) >= 2:
        return "Intervalo"
    if len(times) == 1:
        return "Horario"
    if "manha" in text:
        return "Manha"
    if "tarde" in text:
        return "Tarde"
    if "noite" in text:
        return "Noite"
    return "Generica"


def description_agenda_info(row):
    category = description_info_category(row)
    if category == "Sem agenda desc":
        return ""
    times = sorted(set(extract_minutes_from_description(row)))
    if category == "Intervalo":
        return f" | INFO DESC: INTERVALO {fmt_minutes(times[0])}-{fmt_minutes(times[-1])}"
    if category == "Horario":
        return f" | INFO DESC: HORA {fmt_minutes(times[0])}"
    if category in {"Manha", "Tarde", "Noite"}:
        return f" | INFO DESC: TURNO {category.upper()}"
    info = description_agenda_text(row)
    return f" | INFO DESC: {info[:48].upper()}"


def turn_chart_lines(rows):
    counts = Counter(description_info_category(row) for row in rows)
    total = len(rows)
    lines = ["", "Grafico - info encontrada na descricao:"]
    for label in ("Manha", "Tarde", "Noite", "Horario", "Intervalo", "Generica"):
        count = counts.get(label, 0)
        if count:
            lines.append(f"@BAR|{label}|{count}|{total}")
    if len(lines) == 1:
        lines.append("- Nenhuma OS com turno, horario ou info generica na descricao.")
    return lines


def grouped_snapshot_rows(rows):
    grouped = {}
    buckets = defaultdict(list)
    for row in rows:
        buckets[row.get("filial_nome") or target_name(row) or "Sem filial"].append(row)
    for filial, group in sorted(buckets.items()):
        grouped[filial] = sorted(group, key=lambda row: row.get("data_agenda") or "")
    return grouped


def add_row_start_intervals(compact_rows):
    rows = [dict(row) for row in compact_rows]
    grouped = defaultdict(list)
    for index, row in enumerate(rows):
        started = compact_start_dt(row)
        agenda_day = compact_agenda_day(row)
        if not started or not agenda_day or started.date() != agenda_day:
            continue
        grouped[
            (
                row.get("filial_nome") or "Sem filial",
                row.get("tecnico") or "Sem tecnico",
                started.date().isoformat(),
            )
        ].append((index, row, started))

    for items in grouped.values():
        ordered = sorted(items, key=lambda item: (item[2], item[1].get("id") or ""))
        total = len(ordered)
        for position, (index, row, started) in enumerate(ordered, start=1):
            previous_item = ordered[position - 2] if position > 1 else None
            next_item = ordered[position] if position < total else None
            previous_started = previous_item[2] if previous_item else None
            next_started = next_item[2] if next_item else None
            rows[index].update(
                {
                    "ordem_inicio_tecnico_cidade": position,
                    "total_inicios_tecnico_cidade_no_dia": total,
                    "os_inicio_anterior": previous_item[1].get("id") if previous_item else None,
                    "inicio_anterior": previous_started.isoformat(timespec="seconds")
                    if previous_started
                    else None,
                    "intervalo_desde_inicio_anterior_min": minutes_between(previous_started, started)
                    if previous_started
                    else None,
                    "os_proximo_inicio": next_item[1].get("id") if next_item else None,
                    "proximo_inicio": next_started.isoformat(timespec="seconds")
                    if next_started
                    else None,
                    "intervalo_ate_proximo_inicio_min": minutes_between(started, next_started)
                    if next_started
                    else None,
                }
            )
    return rows


def snapshot_summary(compact_rows):
    by_city = {}
    attention = []
    for city, rows in grouped_snapshot_rows(compact_rows).items():
        status_counter = Counter(row.get("status") for row in rows)
        agenda_counter = Counter(
            (row.get("cumprimento_agenda") or {}).get("status") or "sem analise"
            for row in rows
        )
        by_city[city] = {
            "total": len(rows),
            "status": dict(sorted(status_counter.items())),
            "cumprimento_agenda": dict(sorted(agenda_counter.items())),
            "com_turno_ou_horario_na_descricao": sum(
                1
                for row in rows
                if row.get("agenda_descricao")
                not in {"sem descricao", "sem agenda na descricao"}
            ),
            "sem_turno_ou_horario_na_descricao": sum(
                1
                for row in rows
                if row.get("agenda_descricao")
                in {"sem descricao", "sem agenda na descricao"}
            ),
            "tecnicos": technician_allocation_summary(rows),
        }
    for row in compact_rows:
        compliance = row.get("cumprimento_agenda") or {}
        if compliance.get("nivel") in {"critico", "atencao"}:
            attention.append(
                {
                    "id": row.get("id"),
                    "filial_nome": row.get("filial_nome"),
                    "status": row.get("status"),
                    "tecnico": row.get("tecnico"),
                    "assunto": row.get("assunto"),
                    "agenda": row.get("data_agenda"),
                    "agenda_final": row.get("data_agenda_final"),
                    "sinal": compliance.get("emoji"),
                    "cumprimento": compliance.get("status"),
                    "motivo": compliance.get("motivo"),
                }
            )
    return {
        "total": len(compact_rows),
        "por_cidade": by_city,
        "sinais": {
            "✅": "cumprido/finalizado dentro do corte",
            "🕒": "aguardando janela",
            "🟡": "sem turno/horario/data clara na descricao",
            "⚠️": "agenda em risco ou divergente",
            "❌": "agendamento da descricao nao cumprido pelo criterio atual",
        },
        "atencao": attention,
    }


def minutes_between(previous, current):
    return round((current - previous).total_seconds() / 60)


def compact_start_dt(row):
    return parse_dt(row.get("data_inicio")) or parse_dt(row.get("data_hora_execucao"))


def compact_agenda_day(row):
    agenda = parse_dt(row.get("data_agenda"))
    return agenda.date() if agenda else None


def technician_allocation_summary(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("tecnico") or "Sem tecnico"].append(row)

    summary = []
    for technician, items in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        subject_counts = Counter((row.get("assunto") or "Sem assunto").strip() for row in items)
        started = [
            {
                "id": row.get("id"),
                "inicio": compact_start_dt(row),
            }
            for row in items
            if compact_start_dt(row)
            and compact_agenda_day(row)
            and compact_start_dt(row).date() == compact_agenda_day(row)
        ]
        started = sorted(started, key=lambda item: item["inicio"])
        intervals = []
        for previous, current in zip(started, started[1:]):
            intervals.append(
                {
                    "de_os": previous["id"],
                    "para_os": current["id"],
                    "inicio_anterior": previous["inicio"].isoformat(timespec="seconds"),
                    "inicio_atual": current["inicio"].isoformat(timespec="seconds"),
                    "minutos": minutes_between(previous["inicio"], current["inicio"]),
                }
            )
        interval_values = [item["minutos"] for item in intervals]
        summary.append(
            {
                "tecnico": technician,
                "total_os": len(items),
                "assuntos": dict(sorted(subject_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
                "os_com_inicio_no_dia": len(started),
                "os_sem_inicio_no_dia": len(items) - len(started),
                "os_com_inicio_registrado": len(started),
                "os_sem_inicio_registrado": len(items) - len(started),
                "intervalos_entre_inicios_min": intervals,
                "intervalo_medio_min": round(sum(interval_values) / len(interval_values), 1)
                if interval_values
                else None,
                "menor_intervalo_min": min(interval_values) if interval_values else None,
                "maior_intervalo_min": max(interval_values) if interval_values else None,
            }
        )
    return summary


def browser_candidates():
    configured = os.environ.get("BROWSER") or os.environ.get("CHROME_BIN")
    if configured:
        yield Path(configured)
    for env_name, relative in [
        ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
        ("ProgramFiles(x86)", "Google/Chrome/Application/chrome.exe"),
        ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"),
        ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
    ]:
        base = os.environ.get(env_name)
        if base:
            yield Path(base) / relative
    for command in ["chrome", "google-chrome", "chromium", "msedge"]:
        yield Path(command)


def find_browser():
    for candidate in browser_candidates():
        if candidate.exists() or not candidate.is_absolute():
            return str(candidate)
    return None


def render_png(html_path, png_path, height=1800):
    browser = find_browser()
    if not browser:
        return "Chrome/Edge nao encontrado para gerar imagem."
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1400," + str(height),
        f"--screenshot={png_path}",
        html_path.as_uri(),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=90)
    except Exception as exc:
        return f"Falha ao gerar imagem: {exc}"
    return None


def h(value):
    return html.escape(str(value if value is not None else ""))


def classify_operation(score):
    if score >= 90:
        return "Excelente"
    if score >= 80:
        return "Muito Bom"
    if score >= 70:
        return "Bom"
    if score >= 60:
        return "Atencao"
    return "Critico"


def tech_start_intervals(items, day_start, cutoff_dt):
    ordered = sorted(
        items,
        key=lambda row: parse_dt(row.get("data_inicio"))
        or parse_dt(row.get("data_hora_execucao"))
        or parse_dt(row.get("data_fechamento"))
        or datetime.max.replace(tzinfo=TZ),
    )
    starts = []
    for row in ordered:
        started = parse_dt(row.get("data_inicio")) or parse_dt(row.get("data_hora_execucao"))
        if started and day_start <= started <= cutoff_dt:
            starts.append(started)
    return [round((current - previous).total_seconds() / 60) for previous, current in zip(starts, starts[1:])]


def interval_summary(intervals):
    if not intervals:
        return {"avg": None, "min": None, "max": None}
    return {
        "avg": round(sum(intervals) / len(intervals), 1),
        "min": min(intervals),
        "max": max(intervals),
    }


def format_interval(value):
    return "-" if value is None else f"{value:.1f} min" if isinstance(value, float) else f"{value} min"


def historical_productivity(day, cutoff):
    suffix = cutoff[:5].replace(":", "")
    current = datetime.strptime(day, "%Y-%m-%d").date()
    samples = []
    for path in sorted(SNAPSHOT_DIR.glob(f"snapshot_os_midday_*_{suffix}.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            sample_day = datetime.strptime(str(payload.get("day")), "%Y-%m-%d").date()
            if sample_day >= current:
                continue
            initial = int(payload.get("initial_total") or 0)
            done = int(payload.get("finished_until_cutoff") or 0)
            if initial:
                samples.append({"day": sample_day.isoformat(), "productivity": done / initial * 100})
        except Exception:
            continue
    return samples[-5:]


def build_monitor_payload(day, cutoff, source, rows, finished, open_rows, funcionarios, assuntos, cutoff_dt, day_start):
    initial_total = len(rows)
    productivity = (len(finished) / initial_total * 100) if initial_total else 0
    flow = operation_flow(rows, finished, open_rows, day_start, cutoff_dt)
    attention = sla_attention(open_rows, cutoff_dt, funcionarios, assuntos, limit=8)
    supervisor_items = supervisor_queue(open_rows, cutoff_dt, day_start, funcionarios, assuntos, limit=10)
    window_overdue = sum(1 for item in attention if any("janela vencida" in reason for reason in item["reasons"]))

    city_rows = []
    for filial, group in sorted(by_filial(rows).items()):
        done = [row for row in finished if (target_name(row) or "Sem filial") == filial]
        started = [row for row in group if started_by_cutoff(row, day_start, cutoff_dt)]
        started_ids = {str(row.get("id")) for row in started}
        pending = len(group) - len(done)
        no_start = len([row for row in group if str(row.get("id")) not in started_ids and row not in done])
        percent = (len(done) / len(group) * 100) if group else 0
        city_rows.append(
            {
                "city": filial,
                "initial": len(group),
                "started": len(started),
                "finished": len(done),
                "no_start": no_start,
                "pending": pending,
                "productivity": round(percent, 1),
            }
        )
    city_rows.sort(key=lambda item: item["productivity"], reverse=True)

    tech_groups = defaultdict(list)
    work_rows = [row for row in rows if started_by_cutoff(row, day_start, cutoff_dt)]
    for row in work_rows:
        tech_groups[str(row.get("id_tecnico") or "0")].append(row)
    tech_rows = []
    all_intervals = []
    for tech_id, items in tech_groups.items():
        intervals = tech_start_intervals(items, day_start, cutoff_dt)
        all_intervals.extend(intervals)
        summary = interval_summary(intervals)
        avg = summary["avg"]
        done_count = sum(1 for row in items if row in finished)
        open_started = len(items) - done_count
        if len(items) >= 3 and (avg is None or avg <= 90) and open_started <= 1:
            rating = "Excelente"
        elif len(items) >= 2 and (avg is None or avg <= 150):
            rating = "Regular"
        else:
            rating = "Atencao"
        tech_rows.append(
            {
                "name": funcionarios.get(tech_id, f"ID {tech_id}"),
                "started": len(items),
                "done": done_count,
                "open_started": open_started,
                "avg": avg,
                "min": summary["min"],
                "max": summary["max"],
                "rating": rating,
            }
        )
    tech_rows.sort(
        key=lambda item: (
            -item["done"],
            -item["started"],
            item["avg"] if item["avg"] is not None else 9999,
            item["name"],
        )
    )

    pending_rows = []
    for filial, group in sorted(by_filial(open_rows).items()):
        counts = status_counts(group)
        pending_rows.append(
            {
                "city": filial,
                "ag": counts.get("AG", 0),
                "exec": counts.get("EX", 0) + counts.get("EP", 0),
                "other": sum(count for status, count in counts.items() if status not in {"AG", "EX", "EP"}),
                "total": len(group),
            }
        )

    cutoff_hour = int(cutoff[:2])
    target_productivity = 60 if cutoff_hour < 19 else 85
    execution_progress = (flow["started"] / initial_total * 100) if initial_total else 0
    finish_score = min(productivity / target_productivity * 100, 100) if target_productivity else 0
    progress_score = min(execution_progress / target_productivity * 100, 100) if target_productivity else 0
    if cutoff_hour < 19:
        productivity_score = finish_score * 0.70 + progress_score * 0.30
    else:
        productivity_score = finish_score * 0.90 + progress_score * 0.10
    sla_score = max(0, 100 - len(attention) * 14 - window_overdue * 8)
    pending_score = max(0, 100 - ((len(open_rows) / initial_total * 100) if initial_total else 0))
    avg_interval = statistics.mean(all_intervals) if all_intervals else None
    fluency_score = 75 if avg_interval is None and work_rows else max(0, 100 - max((avg_interval or 0) - 60, 0) * 0.6)
    score = round(productivity_score * 0.35 + sla_score * 0.30 + pending_score * 0.20 + fluency_score * 0.15, 1)

    alerts = []
    if attention:
        alerts.append(f"{len(attention)} OS em atencao SLA/agenda; {window_overdue} com janela vencida.")
    if flow["not_started"]:
        alerts.append(f"{flow['not_started']} OS sem inicio registrado ate o corte.")
    if flow["started_not_finished"]:
        alerts.append(f"{flow['started_not_finished']} OS iniciadas sem finalizacao; validar baixa/registro no IXC.")
    if city_rows and city_rows[-1]["productivity"] < 60:
        alerts.append(f"{city_rows[-1]['city']} com produtividade baixa ({city_rows[-1]['productivity']:.1f}%).")
    if tech_rows:
        slow = max(tech_rows, key=lambda item: item["avg"] if item["avg"] is not None else -1)
        if slow["avg"] and slow["avg"] >= 150:
            alerts.append(f"{first_name(slow['name'])} com intervalo medio alto ({slow['avg']:.1f} min).")
    if len(open_rows) >= max(3, initial_total * 0.25):
        alerts.append(f"Backlog pendente relevante: {len(open_rows)} OS nao finalizadas.")
    if not alerts:
        alerts.append("Operacao sem alerta critico automatico no criterio atual.")
    alerts = alerts[:5]

    trends = []
    history = historical_productivity(day, cutoff)
    if len(history) >= 3:
        avg_hist = statistics.mean(item["productivity"] for item in history)
        delta = productivity - avg_hist
        if abs(delta) >= 10:
            direction = "positiva" if delta > 0 else "queda"
            trends.append(f"Tendencia {direction}: produtividade {abs(delta):.1f} p.p. {'acima' if delta > 0 else 'abaixo'} da media recente.")
    if not trends:
        trends.append("Historico insuficiente ou sem variacao relevante para tendencia segura.")

    diagnosis = []
    actions = []
    if cutoff_hour >= 19:
        if productivity >= target_productivity:
            diagnosis.append("Ponto positivo: produtividade geral dentro da meta do fechamento.")
        else:
            diagnosis.append("Ponto de atencao: produtividade geral abaixo da meta do fechamento.")
        if attention:
            diagnosis.append("Risco: SLA/janelas pendentes podem virar backlog e retrabalho.")
            actions.append("Alta: cobrar resolucao das OS em atencao antes de abrir nova demanda.")
        if city_rows and city_rows[-1]["pending"]:
            actions.append(f"Media: revisar pendencias da filial {city_rows[-1]['city']} no proximo planejamento.")
        actions.append("Baixa: usar ranking de fluidez para ajustar distribuicao de agenda.")
    else:
        diagnosis.append("Parcial do dia: usar apenas para correcao de rota, nao como fechamento de desempenho.")
        if flow["not_started"]:
            actions.append("Alta: confirmar saida/rota das OS sem inicio registrado.")
        if flow["started_not_finished"]:
            actions.append("Alta: validar se OS iniciadas precisam de baixa/atualizacao no IXC.")
        if attention:
            actions.append("Alta: acionar agora responsaveis pelas OS com SLA/janela vencida.")
        else:
            actions.append("Media: manter acompanhamento ate o fechamento.")

    return {
        "day": day,
        "cutoff": cutoff[:5],
        "source": source,
        "initial_total": initial_total,
        "finished": len(finished),
        "pending": len(open_rows),
        "productivity": round(productivity, 1),
        "execution_progress": round(execution_progress, 1),
        "flow": flow,
        "active_techs": len(tech_rows),
        "attention_count": len(attention),
        "window_overdue": window_overdue,
        "score": score,
        "classification": classify_operation(score),
        "city_rows": city_rows,
        "tech_rows": tech_rows,
        "attention": attention,
        "supervisor_queue": supervisor_items,
        "pending_rows": pending_rows,
        "alerts": alerts,
        "trends": trends[:3],
        "diagnosis": diagnosis[:4],
        "actions": actions[:4],
    }


def monitor_html_path(day, cutoff):
    suffix = cutoff[:5].replace(":", "")
    return SNAPSHOT_DIR / f"monitor_tecnico_executivo_{day}_{suffix}.html"


def monitor_png_path(day, cutoff):
    return monitor_html_path(day, cutoff).with_suffix(".png")


def build_monitor_html(payload, path):
    def rows_html(items, columns):
        body = []
        for item in items:
            body.append("<tr>" + "".join(f"<td>{h(item.get(key, ''))}</td>" for key, _label in columns) + "</tr>")
        return "".join(body) or f"<tr><td colspan='{len(columns)}'>Sem dados.</td></tr>"

    city_columns = [
        ("city", "Cidade"),
        ("initial", "Inicial"),
        ("started", "Inic."),
        ("finished", "Fim"),
        ("no_start", "Sem inicio"),
        ("pending", "Pend"),
        ("productivity", "%"),
    ]
    pending_columns = [("city", "Cidade"), ("ag", "AG"), ("exec", "Exec"), ("other", "Outros"), ("total", "Total")]
    tech_cards = []
    for index, item in enumerate(payload["tech_rows"][:8], start=1):
        medal = "1" if index == 1 else "2" if index == 2 else "3" if index == 3 else str(index)
        summary = (
            f"iniciou {item['started']} | finalizou {item['done']} | "
            f"abertas {item['open_started']} | media {format_interval(item['avg'])}"
        )
        tech_cards.append(
            f"""
            <div class="tech">
              <div class="rank">{medal}</div>
              <div><b>{h(item['name'])}</b><span>{h(summary)}</span></div>
              <strong class="{h(item['rating']).lower()}">{h(item['rating'])}</strong>
            </div>
            """
        )

    attention_rows = []
    for item in payload["attention"][:6]:
        attention_rows.append(
            f"<tr><td>{h(item['id'])}</td><td>{h(item['filial'])}</td><td>{h(first_name(item['tecnico']))}</td><td>{h(item['assunto'])}</td><td>{h('; '.join(item['reasons']))}</td></tr>"
        )
    attention_html = "".join(attention_rows) or "<tr><td colspan='5'>Nenhuma OS em atencao de SLA.</td></tr>"
    supervisor_columns = [
        ("id", "OS"),
        ("filial", "Cidade"),
        ("tecnico", "Tec."),
        ("caixa", "Caixa"),
        ("acao", "Acao"),
        ("motivo", "Motivo"),
    ]

    closing_sections = ""
    if int(payload["cutoff"][:2]) >= 19:
        closing_sections = f"""
        <section class="panel split">
          <div><h2>Diagnostico</h2>{''.join(f'<p>{h(item)}</p>' for item in payload['diagnosis'])}</div>
          <div><h2>Acoes</h2>{''.join(f'<p>{h(item)}</p>' for item in payload['actions'])}</div>
        </section>
        """

    flow = payload["flow"]
    html_text = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <title>Monitor Tecnico Executivo</title>
  <style>
    :root {{ --bg:#13211f; --panel:#fdfdfb; --line:#d8ded8; --text:#1f2933; --muted:#64748b; --green:#047857; --orange:#b45309; --red:#b91c1c; --blue:#2563eb; --soft:#f3f6f1; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; color:var(--text); font-family:Inter,Segoe UI,Arial,sans-serif; background:var(--soft); }}
    header {{ padding:26px 28px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:flex-end; }}
    h1 {{ margin:0; font-size:24px; }} h2 {{ margin:0 0 12px; font-size:17px; }} p {{ color:var(--muted); margin:5px 0; font-size:13px; }} .wrap {{ padding:22px; display:grid; gap:16px; }}
    .kpis {{ display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:10px; }} .kpi,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:0 12px 38px rgba(31,41,51,.10); }}
    .kpi {{ padding:13px; }} .kpi span,td,th,.tech span {{ color:var(--muted); font-size:12px; }} .kpi b {{ display:block; margin-top:6px; font-size:22px; }}
    .score {{ color:var(--green); }} .score.warn {{ color:var(--orange); }} .score.bad {{ color:var(--red); }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }} .panel {{ padding:16px; }} table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:8px; border-bottom:1px solid var(--line); }} th {{ color:var(--text); background:#eef2f0; }}
    .tech {{ display:grid; grid-template-columns:34px 1fr 86px; gap:10px; align-items:center; padding:9px 0; border-bottom:1px solid var(--line); }} .rank {{ width:28px; height:28px; border-radius:50%; background:#dbeafe; display:grid; place-items:center; color:var(--blue); font-weight:800; }} .tech b,.tech span {{ display:block; }} .tech strong {{ text-align:right; font-size:12px; }} .excelente {{ color:var(--green); }} .regular {{ color:var(--orange); }} .atencao {{ color:var(--red); }}
    .alerts {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }} .alert {{ padding:9px 0; border-bottom:1px solid var(--line); color:var(--orange); font-size:13px; }} .split {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
    footer {{ color:var(--muted); font-size:12px; padding:0 22px 22px; }}
  </style>
</head>
<body>
  <header>
    <div><h1>Monitor Tecnico Executivo</h1><p>{h(payload['day'])} · {h(payload['cutoff'])} · base: {h(payload['source'])}</p></div>
    <div><h1 class="score {'bad' if payload['score'] < 60 else 'warn' if payload['score'] < 80 else ''}">{payload['score']}</h1><p>{h(payload['classification'])}</p></div>
  </header>
  <main class="wrap">
    <section class="kpis">
      <div class="kpi"><span>OS iniciais</span><b>{payload['initial_total']}</b></div>
      <div class="kpi"><span>Iniciadas</span><b>{flow['started']}</b></div>
      <div class="kpi"><span>Finalizadas</span><b>{payload['finished']}</b></div>
      <div class="kpi"><span>Sem inicio</span><b>{flow['not_started']}</b></div>
      <div class="kpi"><span>Pendentes</span><b>{payload['pending']}</b></div>
      <div class="kpi"><span>Produtividade</span><b>{payload['productivity']:.1f}%</b></div>
      <div class="kpi"><span>SLA atencao</span><b>{payload['attention_count']}</b></div>
      <div class="kpi"><span>Janelas vencidas</span><b>{payload['window_overdue']}</b></div>
    </section>
    <section class="grid">
      <div class="panel"><h2>Performance por Cidade</h2><table><thead><tr>{''.join(f'<th>{label}</th>' for _key,label in city_columns)}</tr></thead><tbody>{rows_html(payload['city_rows'], city_columns)}</tbody></table></div>
      <div class="panel"><h2>Ranking e Fluidez dos Tecnicos</h2>{''.join(tech_cards) or '<p>Nenhum tecnico com OS finalizada.</p>'}</div>
    </section>
    <section class="grid">
      <div class="panel"><h2>SLA e Janelas em Atencao</h2><table><thead><tr><th>OS</th><th>Cidade</th><th>Tec.</th><th>Assunto</th><th>Motivo</th></tr></thead><tbody>{attention_html}</tbody></table></div>
      <div class="panel"><h2>Pendencias por Cidade</h2><table><thead><tr>{''.join(f'<th>{label}</th>' for _key,label in pending_columns)}</tr></thead><tbody>{rows_html(payload['pending_rows'], pending_columns)}</tbody></table></div>
    </section>
    <section class="panel">
      <h2>Fila do Supervisor Tecnico</h2>
      <table><thead><tr>{''.join(f'<th>{label}</th>' for _key,label in supervisor_columns)}</tr></thead><tbody>{rows_html(payload['supervisor_queue'], supervisor_columns)}</tbody></table>
    </section>
    <section class="alerts">
      <div class="panel"><h2>Alertas Automaticos</h2>{''.join(f'<div class="alert">{h(item)}</div>' for item in payload['alerts'])}</div>
      <div class="panel"><h2>Tendencias</h2>{''.join(f'<div class="alert">{h(item)}</div>' for item in payload['trends'])}</div>
    </section>
    {closing_sections}
  </main>
  <footer>Score: produtividade/progresso 35%, SLA 30%, pendencias 20%, fluidez 15%. Sem dados de cliente.</footer>
</body>
</html>"""
    path.write_text(html_text, encoding="utf-8")


def draw_pdf(lines, title, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    margin = 42
    y = height - margin
    pdf.setTitle(title)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(margin, y, title[:95])
    y -= 22
    pdf.setFont("Helvetica", 8.5)
    line_height = 12
    max_chars = 118
    for line in lines:
        line = (
            str(line)
            .replace("✅", "[OK]")
            .replace("🕒", "[AGUARDANDO]")
            .replace("🟡", "[SEM OBS]")
            .replace("⚠️", "[ALERTA]")
            .replace("⚠", "[ALERTA]")
            .replace("❌", "[NAO CUMPRIDO]")
        )
        if line.startswith("@BAR|"):
            _, label, count, total = line.split("|", 3)
            count = int(count)
            total = max(int(total), 1)
            if y < margin + 24:
                pdf.showPage()
                y = height - margin
                pdf.setFont("Helvetica", 8.5)
            bar_x = margin + 72
            bar_y = y - 2
            bar_width = 260
            fill_width = int(bar_width * (count / total))
            pdf.setFillColorRGB(0.92, 0.92, 0.92)
            pdf.rect(bar_x, bar_y, bar_width, 8, fill=1, stroke=0)
            pdf.setFillColorRGB(0.18, 0.38, 0.72)
            pdf.rect(bar_x, bar_y, fill_width, 8, fill=1, stroke=0)
            pdf.setFillColorRGB(0, 0, 0)
            pdf.drawString(margin, y, label)
            pdf.drawString(bar_x + bar_width + 8, y, str(count))
            y -= 16
            continue
        parts = [""]
        if line:
            parts = [line[i : i + max_chars] for i in range(0, len(line), max_chars)]
        for part in parts:
            if y < margin:
                pdf.showPage()
                y = height - margin
                pdf.setFont("Helvetica", 8.5)
            if part.endswith(":") and not part.startswith("-"):
                pdf.setFont("Helvetica-Bold", 9)
                pdf.drawString(margin, y, part)
                pdf.setFont("Helvetica", 8.5)
            else:
                pdf.drawString(margin, y, part)
            y -= line_height
    pdf.save()
    return path


def report_pdf_path(day, mode):
    return SNAPSHOT_DIR / f"relatorio_os_{mode}_{day}.pdf"


def report_txt_path(day, mode):
    return SNAPSHOT_DIR / f"relatorio_os_{mode}_{day}.txt"


def midday_snapshot_path(day, cutoff):
    suffix = cutoff[:5].replace(":", "")
    return SNAPSHOT_DIR / f"snapshot_os_midday_{day}_{suffix}.json"


def morning_pdf_lines(day, agenda, funcionarios, assuntos, clientes, start_cutoff):
    lines = morning_command_snapshot(day, agenda, funcionarios, assuntos, start_cutoff)
    lines.extend([
        "",
        f"OS agendadas - inicio do dia ({day})",
        f"Total inicial: {len(agenda)} OS",
        "Sinais: OK=cumprido | AGUARDANDO=janela da descricao futura | SEM DESC=sem turno/horario na descricao | ALERTA=risco | NAO CUMPRIDO=vencido",
        "",
        "Resumo por cidade:",
    ])
    for filial, rows in sorted(by_filial(agenda).items()):
        with_description = sum(1 for row in rows if has_description_agenda(row))
        lines.append(
            f"- {filial}: {len(rows)} OS | com turno/horario na descricao: {with_description} | sem turno/horario na descricao: {len(rows) - with_description} | status: {format_status(status_counts(rows))}"
        )

    lines.extend(turn_chart_lines(agenda))
    compact_for_summary = [compact_row(row, funcionarios, assuntos, clientes, start_cutoff) for row in agenda]
    lines.extend(["", "Tecnicos, assuntos e intervalo entre inicios:"])
    for filial, city_payload in snapshot_summary(compact_for_summary)["por_cidade"].items():
        lines.append(f"{filial}:")
        for item in city_payload["tecnicos"]:
            lines.append(
                f"- {first_name(item['tecnico'])}: {item['total_os']} OS | assuntos: {format_subject_summary(item['assuntos'])} | inicios hoje: {item['os_com_inicio_no_dia']} reg., {item['os_sem_inicio_no_dia']} sem reg. | {format_interval_summary(item)}"
            )
    lines.extend(["", "OS agendadas por cidade:", "Formato: TECNICO -> OS | ASSUNTO | AGENDA DESCRICAO | RESULTADO"])
    for filial, rows in sorted(by_filial(agenda).items()):
        lines.append(f"{filial}:")
        if not rows:
            lines.append("- Nenhuma OS.")
            continue
        for tech_id, tech_rows in sorted(
            by_technician(rows).items(),
            key=lambda item: (funcionarios.get(item[0], f"ID {item[0]}"), item[0]),
        ):
            tech_name = funcionarios.get(tech_id, f"ID {tech_id}")
            lines.append(f"{first_name(tech_name)} - {len(tech_rows)} OS:")
            for row in sorted(tech_rows, key=os_sort_key):
                compliance = schedule_compliance(row, start_cutoff)
                subject = assuntos.get(str(row.get("id_assunto") or ""), str(row.get("id_assunto") or ""))
                lines.append(
                    f"- {compliance['emoji']} OS {row.get('id')} | {subject} | Agenda: {clean_schedule_text(row)} | Resultado: {clean_result_text(compliance)}"
                )

    attention_count = len(sla_attention(agenda, start_cutoff, funcionarios, assuntos))
    lines.extend(["", f"Atencao SLA/agenda as 8h: {attention_count} OS"])
    return lines


def morning_command_snapshot(day, agenda, funcionarios, assuntos, start_cutoff):
    total = len(agenda)
    attention = sla_attention(agenda, start_cutoff, funcionarios, assuntos, limit=max(total, 12))
    attention_by_city = Counter(item.get("filial") for item in attention)
    with_description = sum(1 for row in agenda if has_description_agenda(row))
    without_description = total - with_description
    overdue_description = [
        row
        for row in agenda
        if ((schedule_compliance(row, start_cutoff).get("status") or "").startswith("nao cumprido"))
    ]
    city_rows = []
    for filial, rows in sorted(by_filial(agenda).items()):
        city_rows.append(
            {
                "filial": filial,
                "total": len(rows),
                "com_agenda": sum(1 for row in rows if has_description_agenda(row)),
                "sem_agenda": sum(1 for row in rows if not has_description_agenda(row)),
                "atencao": attention_by_city.get(filial, 0),
            }
        )
    city_rows.sort(key=lambda item: (item["atencao"], item["total"], item["sem_agenda"]), reverse=True)

    tech_loads = []
    for tech_id, rows in by_technician(agenda).items():
        tech_loads.append(
            {
                "tecnico": funcionarios.get(tech_id, f"ID {tech_id}"),
                "total": len(rows),
                "filial": Counter(target_name(row) or "Sem filial" for row in rows).most_common(1)[0][0],
            }
        )
    tech_loads.sort(key=lambda item: item["total"], reverse=True)

    if total and (len(attention) >= max(5, total * 0.45) or len(overdue_description) >= 3):
        risk = "CRITICO"
    elif attention or (total and without_description / total >= 0.4):
        risk = "ATENCAO"
    else:
        risk = "CONTROLADO"

    top_city = city_rows[0] if city_rows else None
    top_tech = tech_loads[0] if tech_loads else None
    clarity = f"{with_description}/{total}" if total else "0/0"
    clarity_pct = fmt_percent((with_description / total * 100) if total else 0)

    lines = [
        f"SNAPSHOT EXECUTIVO - OS TECNICAS - {day}",
        f"Risco: {risk}",
        f"Volume: {total} OS | agenda clara: {clarity} ({clarity_pct}%) | sem agenda clara: {without_description}",
        f"SLA/agenda em atencao: {len(attention)} OS | agenda nao cumprida: {len(overdue_description)} OS",
    ]
    if top_city:
        lines.append(
            f"Gargalo por cidade: {top_city['filial']} ({top_city['total']} OS, {top_city['atencao']} em atencao, {top_city['sem_agenda']} sem agenda clara)"
        )
    if top_tech:
        lines.append(
            f"Tecnico mais carregado: {first_name(top_tech['tecnico'])} ({top_tech['total']} OS, principal cidade: {top_tech['filial']})"
        )

    actions = []
    if top_city and top_city["atencao"]:
        actions.append(f"acionar supervisor em {top_city['filial']} para validar OS em atencao")
    if top_tech and top_tech["total"] >= 6:
        actions.append(f"revisar carga de {first_name(top_tech['tecnico'])}")
    if without_description:
        actions.append("corrigir OS sem turno/horario claro antes do meio-dia")
    if overdue_description:
        actions.append("separar agenda vencida entre atraso real e baixa/registro no IXC")
    if not actions:
        actions.append("acompanhar corte de 13h sem acao critica imediata")
    lines.append("Acao imediata: " + "; ".join(actions[:3]) + ".")

    lines.append("")
    lines.append("Top prioridades para supervisor:")
    if not attention:
        lines.append("- Nenhuma OS em atencao pelo criterio atual.")
    for item in attention[:5]:
        lines.append(
            f"- OS {item['id']} | {item['filial']} | {item['status']} | {first_name(item['tecnico'])} | {item['assunto']} | {'; '.join(item['reasons'])}"
        )
    return lines


def morning(day):
    funcionarios, assuntos = load_maps()
    agenda = fetch_agenda(day)
    clientes = fetch_client_names(row.get("id_cliente") for row in agenda)
    start_cutoff = datetime.strptime(f"{day} 08:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    agenda = [
        row
        for row in agenda
        if not (
            row.get("status") == "F"
            and parse_dt(row.get("data_fechamento"))
            and parse_dt(row.get("data_fechamento")) < start_cutoff
        )
    ]
    compact = add_row_start_intervals(
        [compact_row(row, funcionarios, assuntos, clientes, start_cutoff) for row in agenda]
    )
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "date": day,
        "reference_cutoff": start_cutoff.isoformat(timespec="seconds"),
        "target_filiais": TARGET_FILIAIS,
        "resumo_operacional": snapshot_summary(compact),
        "os": compact,
        "os_por_cidade": grouped_snapshot_rows(compact),
    }
    main_snapshot_path = snapshot_path(day)
    backup_path = preserve_existing_snapshot(main_snapshot_path)
    main_snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pdf_path = draw_pdf(
        morning_pdf_lines(day, agenda, funcionarios, assuntos, clientes, start_cutoff),
        f"OS tecnicas agendadas - inicio do dia - {day}",
        report_pdf_path(day, "morning"),
    )

    lines = [
        f"OS agendadas - inicio do dia ({day})",
        f"Total inicial: {len(agenda)} OS",
        "Sinais: ✅ cumprido | 🕒 aguardando descricao | 🟡 sem turno/horario na descricao | ⚠️ risco | ❌ nao cumprido",
    ]
    for filial, rows in sorted(by_filial(agenda).items()):
        with_description = sum(1 for row in rows if has_description_agenda(row))
        lines.append(
            f"- {filial}: {len(rows)} OS | com turno/horario na descricao: {with_description} | status: {format_status(status_counts(rows))}"
        )
    lines = morning_command_snapshot(day, agenda, funcionarios, assuntos, start_cutoff) + [""] + lines
    lines.append("")
    lines.append("Agendamento informado por cidade:")
    for filial, rows in sorted(by_filial(agenda).items()):
        with_description = sum(1 for row in rows if has_description_agenda(row))
        lines.append(
            f"- {filial}: {len(rows)} OS | com turno/horario/data na descricao: {with_description} | sem turno/horario/data na descricao: {len(rows) - with_description}"
        )
    lines.append("")
    lines.append("Tecnicos, assuntos e intervalo entre inicios:")
    for filial, city_payload in snapshot_summary(compact)["por_cidade"].items():
        lines.append(f"{filial}:")
        for item in city_payload["tecnicos"]:
            lines.append(
                f"- {first_name(item['tecnico'])}: {item['total_os']} OS | assuntos: {format_subject_summary(item['assuntos'])} | inicios hoje: {item['os_com_inicio_no_dia']} reg., {item['os_sem_inicio_no_dia']} sem reg. | {format_interval_summary(item)}"
            )
    lines.append("")
    lines.append("Agenda operacional por cidade:")
    for filial, rows in sorted(by_filial(agenda).items()):
        lines.append(f"{filial}:")
        for tech_id, tech_rows in sorted(
            by_technician(rows).items(),
            key=lambda item: (funcionarios.get(item[0], f"ID {item[0]}"), item[0]),
        ):
            tech_name = funcionarios.get(tech_id, f"ID {tech_id}")
            lines.append(f"{first_name(tech_name)} - {len(tech_rows)} OS:")
            for row in sorted(tech_rows, key=os_sort_key):
                compliance = schedule_compliance(row, start_cutoff)
                subject = assuntos.get(str(row.get("id_assunto") or ""), str(row.get("id_assunto") or ""))
                lines.append(
                    f"- {compliance['emoji']} OS {row.get('id')} | {subject} | Agenda: {clean_schedule_text(row)} | Resultado: {clean_result_text(compliance)}"
                )
    lines.append("")
    lines.append("Detalhamento:")
    lines.append("- Snapshot JSON agora inclui resumo_operacional, cumprimento_agenda por OS e os_por_cidade.")
    lines.append("- Cumprimento de agenda agora considera somente turno, horario, data ou intervalo encontrados na descricao.")
    lines.append("")
    lines.append(f"SLA/agenda em atencao as 8h: {len(sla_attention(agenda, start_cutoff, funcionarios, assuntos))} OS")
    lines.append(f"Snapshot salvo: {main_snapshot_path}")
    if backup_path:
        lines.append(f"Snapshot anterior preservado: {backup_path}")
    lines.append(f"PDF gerado: {pdf_path}")
    return "\n".join(lines)


def midday(day, cutoff):
    funcionarios, assuntos = load_maps()
    path = snapshot_path(day)
    if path.exists():
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        ids = [row["id"] for row in snapshot.get("os", [])]
        initial_total = len(ids)
        rows = fetch_by_ids(ids)
        source = "snapshot das 8h"
    else:
        rows = fetch_agenda(day)
        initial_total = len(rows)
        source = "agenda atual (sem snapshot das 8h)"

    cutoff_dt = datetime.strptime(f"{day} {cutoff}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    day_start = datetime.strptime(f"{day} 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    finished = []
    for row in rows:
        fechamento = parse_dt(row.get("data_fechamento"))
        if row.get("status") == "F" and fechamento and day_start <= fechamento <= cutoff_dt:
            finished.append(row)

    tech_rows = defaultdict(list)
    city_tech_rows = defaultdict(lambda: defaultdict(list))
    for row in finished:
        tech_id = str(row.get("id_tecnico") or "0")
        tech_rows[tech_id].append(row)
        city_tech_rows[target_name(row) or "Sem filial"][tech_id].append(row)

    lines = [
        f"Produtividade tecnica ate {cutoff[:5]} ({day})",
        f"Base: {source}",
        f"Comecamos o dia com: {initial_total} OS",
        f"Finalizadas ate {cutoff[:5]}: {len(finished)} OS ({fmt_percent((len(finished) / initial_total * 100) if initial_total else 0)}%)",
        "Sinais de agenda: ✅ cumprido | 🕒 aguardando descricao | 🟡 sem turno/horario na descricao | ⚠️ risco | ❌ nao cumprido",
    ]
    for filial, group in sorted(by_filial(rows).items()):
        done = [row for row in finished if (target_name(row) or "Sem filial") == filial]
        percent = (len(done) / len(group) * 100) if group else 0
        lines.append(f"- {filial}: {len(done)}/{len(group)} finalizadas ({fmt_percent(percent)}%)")

    lines.append("")
    lines.append("Por cidade/filial e tecnico:")
    for filial in TARGET_FILIAIS.values():
        lines.append(f"{filial}:")
        techs = city_tech_rows.get(filial, {})
        if not techs:
            lines.append("- Nenhum tecnico finalizou OS ate o corte.")
            continue
        for tech_id, items in sorted(
            techs.items(),
            key=lambda kv: (-len(kv[1]), funcionarios.get(kv[0], kv[0])),
        ):
            ordered = sorted(
                items,
                key=lambda row: parse_dt(row.get("data_inicio"))
                or parse_dt(row.get("data_hora_execucao"))
                or parse_dt(row.get("data_fechamento"))
                or datetime.max.replace(tzinfo=TZ),
            )
            starts = []
            for row in ordered:
                started = parse_dt(row.get("data_inicio")) or parse_dt(row.get("data_hora_execucao"))
                if started and day_start <= started <= cutoff_dt:
                    starts.append(started)
            intervals = []
            for previous, current in zip(starts, starts[1:]):
                if previous and current:
                    intervals.append(round((current - previous).total_seconds() / 60))
            interval_text = "-"
            if intervals:
                interval_text = f"media {sum(intervals) / len(intervals):.1f} min | {intervals}"
            os_ids = ", ".join(str(row.get("id")) for row in ordered)
            lines.append(
                f"- {funcionarios.get(tech_id, f'ID {tech_id}')}: {len(ordered)} OS | intervalo entre inicios: {interval_text} | OS: {os_ids}"
            )

    open_rows = [row for row in rows if not completed_by_cutoff(row, cutoff_dt)]
    lines.append("")
    lines.append("Turno/horario na descricao das OS pendentes por cidade:")
    for filial, group in sorted(by_filial(open_rows).items()):
        with_description = sum(1 for row in group if has_description_agenda(row))
        lines.append(
            f"- {filial}: {len(group)} OS | com turno/horario/data na descricao: {with_description} | sem turno/horario/data na descricao: {len(group) - with_description}"
        )
    lines.append("")
    lines.append("OS pendentes com turno/horario/data na descricao:")
    if not open_rows:
        lines.append("- Nenhuma OS pendente na base.")
    for filial, group in sorted(by_filial(open_rows).items()):
        lines.append(f"{filial}:")
        listed = 0
        for row in sorted(group, key=lambda r: parse_dt(r.get("data_agenda")) or datetime.max.replace(tzinfo=TZ)):
            if not has_description_agenda(row):
                continue
            tech_id = str(row.get("id_tecnico") or "0")
            compliance = schedule_compliance(row, cutoff_dt)
            lines.append(
                f"- {compliance['emoji']} OS {row.get('id')} | {row.get('status')} | {funcionarios.get(tech_id, f'ID {tech_id}')} | {description_schedule_pipe(row)} | {compliance['status']}: {compliance['motivo']} | desc: {description_agenda_text(row)}"
            )
            listed += 1
        if not listed:
            lines.append("- Nenhuma OS pendente com agenda na descricao.")
    overdue_description = [
        row
        for row in open_rows
        if (description_due_status(row, cutoff_dt) or "").startswith("nao cumprido")
    ]
    lines.append("")
    lines.append("Agendamento da descricao NAO CUMPRIDO ate o corte:")
    if not overdue_description:
        lines.append("- Nenhuma OS encontrada nesse criterio.")
    for filial, group in sorted(by_filial(overdue_description).items()):
        lines.append(f"{filial}:")
        for row in sorted(group, key=lambda r: parse_dt(r.get("data_agenda")) or datetime.max.replace(tzinfo=TZ)):
            tech_id = str(row.get("id_tecnico") or "0")
            lines.append(
                f"- OS {row.get('id')} | {row.get('status')} | {funcionarios.get(tech_id, f'ID {tech_id}')} | {description_due_status(row, cutoff_dt)} | desc: {description_agenda_text(row)}"
            )
    lines.append("")
    lines.append("SLA/agenda em atencao ate o corte:")
    lines.extend(format_attention(sla_attention(open_rows, cutoff_dt, funcionarios, assuntos)))

    queue_items = supervisor_queue(open_rows, cutoff_dt, day_start, funcionarios, assuntos)
    lines.append("")
    lines.append("Fila do supervisor tecnico:")
    lines.extend(format_supervisor_queue(queue_items))

    lines.append("")
    lines.append(f"Pendentes/nao finalizadas da base: {len(open_rows)} OS")
    for filial, group in sorted(by_filial(open_rows).items()):
        lines.append(f"- {filial}: {len(group)} | status: {format_status(status_counts(group))}")
    report = "\n".join(lines)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = cutoff[:5].replace(":", "")
    clientes = fetch_client_names(row.get("id_cliente") for row in rows)
    snapshot_payload = {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "day": day,
        "cutoff": cutoff,
        "source": source,
        "initial_total": initial_total,
        "finished_until_cutoff": len(finished),
        "pending_until_cutoff": len(open_rows),
        "finished_ids": [str(row.get("id")) for row in finished],
        "pending_ids": [str(row.get("id")) for row in open_rows],
        "fila_supervisor_tecnico": queue_items,
    }
    compact_rows = add_row_start_intervals(
        [compact_row(row, funcionarios, assuntos, clientes, cutoff_dt) for row in rows]
    )
    snapshot_payload["resumo_operacional"] = snapshot_summary(compact_rows)
    snapshot_payload["os"] = compact_rows
    json_path = midday_snapshot_path(day, cutoff)
    txt_path = report_txt_path(day, f"midday_{suffix}")
    pdf_path = draw_pdf(
        lines,
        f"Produtividade tecnica ate {cutoff[:5]} - {day}",
        report_pdf_path(day, f"midday_{suffix}"),
    )
    monitor_payload = build_monitor_payload(
        day,
        cutoff,
        source,
        rows,
        finished,
        open_rows,
        funcionarios,
        assuntos,
        cutoff_dt,
        day_start,
    )
    html_path = monitor_html_path(day, cutoff)
    png_path = monitor_png_path(day, cutoff)
    build_monitor_html(monitor_payload, html_path)
    image_error = render_png(
        html_path,
        png_path,
        height=1400 if int(cutoff[:2]) >= 19 else 1250,
    )
    json_path.write_text(json.dumps(snapshot_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(report, encoding="utf-8")

    output_lines = [
        report,
        "",
        f"Snapshot 13h/periodo salvo: {json_path}",
        f"TXT salvo: {txt_path}",
        f"PDF gerado: {pdf_path}",
        f"HTML executivo gerado: {html_path}",
    ]
    if image_error:
        output_lines.append(f"Imagem nao gerada: {image_error}")
    else:
        output_lines.append(f"Imagem gerada: {png_path}")
    output_lines.extend(
        [
            f"Score operacional: {monitor_payload['score']} ({monitor_payload['classification']})",
            f"Alertas automaticos: {len(monitor_payload['alerts'])}",
        ]
    )
    return "\n".join(output_lines)


def main():
    args = parse_args()
    if args.mode == "morning":
        print(morning(args.date))
    else:
        print(midday(args.date, args.cutoff))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)
