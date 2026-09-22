from __future__ import annotations

import sys
import time
import base64
import hashlib
import json
import secrets
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
ROOT = PROJECT_ROOT if (PROJECT_ROOT / "scripts").exists() else WORKSPACE_ROOT
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
SCRIPTS_DIR = ROOT / "scripts"
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
ACTION_LOG_PATH = LOG_DIR / "ixc-actions.jsonl"
AUDIT_LOG_PATH = LOG_DIR / "audit.jsonl"
SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "cache"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SCALE_DIR = DATA_DIR / "scale"
OS_EXTRA_PATH = DATA_DIR / "os-extra.json"
USERS_PATH = DATA_DIR / "users.json"
sys.path.insert(0, str(SCRIPTS_DIR))

import monitor_os_tecnico_dia as monitor  # noqa: E402


app = FastAPI(title="Kanban OS Tecnicos VIP", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_SECONDS = 30 * 60
_MAP_CACHE: tuple[float, tuple[dict[str, str], dict[str, str]]] | None = None
_TECHNICIAN_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CLIENT_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_OS_PRODUCTS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_PRODUCT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_OS_PRODUCT_QTYPE: str | None = None
MAP_CACHE_SECONDS = 24 * 60 * 60
SNAPSHOT_VERSION = 2
OS_PRODUCT_CACHE_SECONDS = 10 * 60
OS_PRODUCT_QTYPE_CANDIDATES = (
    "movimento_produtos.id_oss_chamado",
    "su_oss_mov_produto.id_chamado",
    "su_oss_mov_produto.id_oss_chamado",
    "su_oss_mov_produto.id_os",
    "su_oss_mov_produto.id_su_oss_chamado",
)
TECHNICIAN_FUNCTION_IDS = {"3", "4", "23"}
EXCLUDED_TECHNICIAN_IDS_BY_FILIAL = {
    "1": {"10", "11"},
}
EXTRA_TECHNICIAN_IDS_BY_FILIAL = {
    # Marcos Vinicius esta ativo e marcado para Kanban na filial 1,
    # mas o cadastro IXC esta sem funcao tecnica preenchida.
    "1": {"126"},
}
TECHNICIAN_SHORT_NAME_OVERRIDES = {
    "126": "Marcos Vinicius",
}

APP_FILIAIS = {
    "1": "VIP TELECOM UDP - Uniao dos Palmares/AL",
    "2": "VIP TELECOM MURICI - Murici/AL",
    "3": "VIP TELECOM QUEIMADAS - Queimadas/PB",
    "4": "GEO TELECOM - Moreno/PE",
    "6": "VIP TELECOM PE - Belo Jardim/PE",
    "7": "NETCITY CORPORATIVO - Belo Jardim/PE",
    "8": "J J DA SILVA TECNOLOGIA - Uniao Corporativo/AL",
    "10": "TOTALNET - Santana do Mundau/AL",
}
monitor.TARGET_FILIAIS = dict(APP_FILIAIS)


class ScheduleRequest(BaseModel):
    data_agendamento: str = Field(..., description="Inicio do agendamento em YYYY-MM-DD HH:MM:SS")
    data_agendamento_final: str | None = Field(default=None, description="Fim da janela em YYYY-MM-DD HH:MM:SS")
    id_tecnico: str | None = None
    mensagem: str | None = None
    status: str = "RAG"


class OsActionRequest(BaseModel):
    acao: str = Field(..., description="Acao do botao Acoes do IXC")
    data_inicio: str | None = Field(default=None, description="Data/hora principal em YYYY-MM-DD HH:MM:SS")
    data_final: str | None = Field(default=None, description="Fim da janela/execucao em YYYY-MM-DD HH:MM:SS")
    id_tecnico: str | None = None
    id_setor: str | None = None
    mensagem: str | None = None
    confirmar_escrita: bool = Field(default=False, description="Quando true, executa a escrita real no IXC.")
    confirmacao: str | None = Field(default=None, description="Frase de confirmacao para escrita real.")


class ScaleItem(BaseModel):
    id_tecnico: str
    tecnico: str | None = None
    tecnico_curto: str | None = None
    situacao: str | None = ""
    funcao: str | None = ""
    dupla_id: str | None = ""
    dupla_nome: str | None = ""
    parceiro_ids: list[str] = Field(default_factory=list)
    observacao: str | None = ""


class ScalePayload(BaseModel):
    date: str
    filial: str
    items: list[ScaleItem] = Field(default_factory=list)


class OsExtraPayload(BaseModel):
    fibra_utilizada_m: int | None = Field(default=None, ge=0, le=100000)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    name: str = Field(..., min_length=2, max_length=80)
    role: str = Field(default="supervisor")
    password: str = Field(..., min_length=6, max_length=80)


class LogoutRequest(BaseModel):
    token: str | None = None


WRITE_CONFIRMATION = "ESCREVER_IXC"
PASSWORD_SALT = "vip-kanban-local-v1"
DEFAULT_USERS = {
    "admin": {"name": "Administrador VIP", "role": "admin", "password": "adminvip2026"},
    "supervisor": {"name": "Supervisor VIP", "role": "supervisor", "password": "supervisorvip2026"},
}
_SESSIONS: dict[str, dict[str, Any]] = {}
SESSION_SECONDS = 12 * 60 * 60


ACTION_DEFS = {
    "agendar": {
        "endpoint": "su_oss_chamado_reagendar",
        "status": "AG",
        "required": ["id_chamado", "data_agendamento", "data_agendamento_final", "status"],
    },
    "marcar_reagendamento": {
        "endpoint": "su_oss_chamado_reagendamento",
        "status": "RAG",
        "required": ["id_chamado", "status", "mensagem"],
    },
    "analisar": {
        "endpoint": "su_oss_chamado_analisar",
        "status": "AN",
        "required": ["id_chamado", "status"],
    },
    "encaminhar": {
        "endpoint": "su_oss_chamado_alterar_setor",
        "status": "EN",
        "required": ["id_chamado", "status"],
    },
    "mensagem": {
        "endpoint": "su_oss_chamado_mensagem",
        "status": "A",
        "required": ["id_chamado", "mensagem", "status", "tipo_cobranca", "finaliza_processo"],
    },
    "executar": {
        "endpoint": "su_oss_chamado_executar",
        "status": "EX",
        "required": ["id_chamado", "data_inicio", "status"],
    },
    "finalizar": {
        "endpoint": "su_oss_chamado_fechar",
        "status": "F",
        "required": ["id_chamado", "data_inicio", "data_final", "status", "mensagem"],
    },
    "reabrir": {
        "endpoint": "su_oss_chamado_reabrir",
        "status": "A",
        "required": ["id_chamado", "status", "mensagem"],
    },
}


STATUS_LABELS = {
    "A": "Aberta",
    "AN": "Em analise",
    "AG": "Agendada",
    "RAG": "Reagendamento",
    "EN": "Encaminhada",
    "EX": "Em execucao",
    "EP": "Em execucao",
    "F": "Finalizada",
}


def parse_day(day: str | None) -> str:
    if not day:
        return date.today().isoformat()
    try:
        return datetime.strptime(day, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Data invalida. Use YYYY-MM-DD.") from exc


def current_reference(day: str) -> datetime:
    today = datetime.now(monitor.TZ).date().isoformat()
    if day == today:
        return datetime.now(monitor.TZ)
    return datetime.combine(
        datetime.strptime(day, "%Y-%m-%d").date(),
        dt_time(hour=19),
        tzinfo=monitor.TZ,
    )


def load_reference_maps() -> tuple[dict[str, str], dict[str, str]]:
    global _MAP_CACHE
    if _MAP_CACHE and time.time() - _MAP_CACHE[0] < MAP_CACHE_SECONDS:
        return _MAP_CACHE[1]
    maps = monitor.load_maps()
    _MAP_CACHE = (time.time(), maps)
    return maps


def write_headers() -> dict[str, str]:
    auth = base64.b64encode(monitor.read_token().encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}


def append_action_log(record: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with ACTION_LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")


def load_os_extra() -> dict[str, dict[str, Any]]:
    if not OS_EXTRA_PATH.exists():
        return {}
    try:
        data = json.loads(OS_EXTRA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def extra_for_os(os_id: str) -> dict[str, Any]:
    data = load_os_extra().get(str(os_id), {})
    return data if isinstance(data, dict) else {}


def save_os_extra(os_id: str, payload: OsExtraPayload, actor: dict[str, Any]) -> dict[str, Any]:
    os_key = str(os_id or "").strip()
    if not os_key:
        raise HTTPException(status_code=400, detail="ID da OS e obrigatorio.")
    data = load_os_extra()
    current = data.get(os_key, {}) if isinstance(data.get(os_key), dict) else {}
    value = payload.fibra_utilizada_m
    if value is None:
        current.pop("fibra_utilizada_m", None)
    else:
        current["fibra_utilizada_m"] = int(value)
    current["updated_at"] = datetime.now(monitor.TZ).isoformat(timespec="seconds")
    current["updated_by"] = public_user(actor)
    data[os_key] = current
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = OS_EXTRA_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(OS_EXTRA_PATH)
    _CACHE.clear()
    return {"id_os": os_key, "extra": current}


def password_hash(password: str) -> str:
    return hashlib.sha256(f"{PASSWORD_SALT}:{password}".encode("utf-8")).hexdigest()


def default_users() -> dict[str, dict[str, str]]:
    return {
        username: {
            "username": username,
            "name": str(data["name"]),
            "role": str(data["role"]),
            "password_hash": password_hash(str(data["password"])),
            "source": "default",
        }
        for username, data in DEFAULT_USERS.items()
    }


def read_local_users() -> dict[str, dict[str, Any]]:
    if not USERS_PATH.exists():
        return {}
    try:
        data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    users_data = data.get("users", {})
    return users_data if isinstance(users_data, dict) else {}


def write_local_users(local_users: dict[str, dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(monitor.TZ).isoformat(timespec="seconds"),
        "users": local_users,
    }
    temp_path = USERS_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(USERS_PATH)


def users() -> dict[str, dict[str, str]]:
    merged = default_users()
    for username, data in read_local_users().items():
        if not isinstance(data, dict):
            continue
        password = str(data.get("password_hash") or "")
        role = str(data.get("role") or "supervisor")
        name = str(data.get("name") or username)
        if not password or role not in {"admin", "supervisor"}:
            continue
        merged[str(username)] = {
            "username": str(username),
            "name": name,
            "role": role,
            "password_hash": password,
            "source": "local",
        }
    return merged


def public_user_record(user: dict[str, Any]) -> dict[str, str]:
    return {
        "username": str(user.get("username") or ""),
        "name": str(user.get("name") or ""),
        "role": str(user.get("role") or ""),
        "source": str(user.get("source") or ""),
    }


def create_local_user(payload: UserCreateRequest, actor: dict[str, Any]) -> dict[str, str]:
    username = payload.username.strip().lower()
    role = payload.role.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
        raise HTTPException(
            status_code=400,
            detail="Usuario deve ter 3 a 40 caracteres: letras, numeros, ponto, hifen ou underline.",
        )
    if role not in {"admin", "supervisor"}:
        raise HTTPException(status_code=400, detail="Perfil deve ser admin ou supervisor.")
    if username in DEFAULT_USERS:
        raise HTTPException(status_code=400, detail="Nao altere usuarios padrao por este painel.")
    local_users = read_local_users()
    local_users[username] = {
        "username": username,
        "name": payload.name.strip(),
        "role": role,
        "password_hash": password_hash(payload.password),
        "created_at": datetime.now(monitor.TZ).isoformat(timespec="seconds"),
        "created_by": public_user(actor),
    }
    write_local_users(local_users)
    return public_user_record({**local_users[username], "source": "local"})


def public_user(session: dict[str, Any]) -> dict[str, str]:
    return {
        "username": str(session.get("username") or ""),
        "name": str(session.get("name") or ""),
        "role": str(session.get("role") or ""),
    }


def append_audit(event: str, actor: dict[str, Any] | None = None, details: dict[str, Any] | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": datetime.now(monitor.TZ).isoformat(timespec="seconds"),
        "event": event,
        "actor": public_user(actor or {}) if actor else {},
        "details": details or {},
    }
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")


def read_audit(limit: int = 100) -> list[dict[str, Any]]:
    if not AUDIT_LOG_PATH.exists():
        return []
    rows = []
    for line in AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))


def require_session(x_vip_session: str | None = Header(default=None)) -> dict[str, Any]:
    token = str(x_vip_session or "").strip()
    session = _SESSIONS.get(token)
    if not token or not session:
        raise HTTPException(status_code=401, detail="Login necessario.")
    if time.time() - float(session.get("created_ts") or 0) > SESSION_SECONDS:
        _SESSIONS.pop(token, None)
        raise HTTPException(status_code=401, detail="Sessao expirada. Faca login novamente.")
    session["last_seen_ts"] = time.time()
    return session


def require_admin(session: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito ao perfil ADM.")
    return session


def cache_key_for(day: str, filial: str, include_client_names: bool) -> str:
    names = "names" if include_client_names else "masked"
    return f"{day}|{filial}|{names}"


def snapshot_path_for(day: str, filial: str, include_client_names: bool) -> Path:
    names = "names" if include_client_names else "masked"
    return SNAPSHOT_DIR / f"kanban-os-{day}-filial-{filial}-{names}.json"


def scale_path_for(day: str, filial: str) -> Path:
    return SCALE_DIR / f"escala-{day}-filial-{filial}.json"


def empty_scale(day: str, filial: str) -> dict[str, Any]:
    return {
        "date": day,
        "filial": str(filial),
        "filial_nome": APP_FILIAIS.get(str(filial), f"Filial {filial}"),
        "updated_at": "",
        "items": [],
    }


def load_scale(day: str, filial: str) -> dict[str, Any]:
    path = scale_path_for(day, filial)
    if not path.exists():
        return empty_scale(day, filial)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_scale(day, filial)
    data.setdefault("date", day)
    data.setdefault("filial", str(filial))
    data.setdefault("filial_nome", APP_FILIAIS.get(str(filial), f"Filial {filial}"))
    data.setdefault("updated_at", "")
    data.setdefault("items", [])
    return data


def save_scale(payload: ScalePayload) -> dict[str, Any]:
    day = parse_day(payload.date)
    filial = str(payload.filial)
    rows = []
    seen: set[str] = set()
    for item in payload.items:
        tech_id = str(item.id_tecnico or "").strip()
        if not tech_id or tech_id in seen:
            continue
        seen.add(tech_id)
        situacao = (item.situacao or "").strip() or "Sem escala"
        funcao = (item.funcao or "").strip()
        if situacao.lower() in {"folga", "ausente", "atestado", "sem escala"}:
            funcao = "Sem escala"
        rows.append(
            {
                "id_tecnico": tech_id,
                "tecnico": (item.tecnico or "").strip(),
                "tecnico_curto": (item.tecnico_curto or "").strip(),
                "situacao": situacao,
                "funcao": funcao,
                "dupla_id": (item.dupla_id or "").strip(),
                "dupla_nome": (item.dupla_nome or "").strip(),
                "parceiro_ids": [str(value) for value in item.parceiro_ids if str(value).strip()],
                "observacao": (item.observacao or "").strip(),
            }
        )
    data = {
        "date": day,
        "filial": filial,
        "filial_nome": APP_FILIAIS.get(filial, f"Filial {filial}"),
        "updated_at": datetime.now(monitor.TZ).isoformat(timespec="seconds"),
        "items": rows,
    }
    SCALE_DIR.mkdir(parents=True, exist_ok=True)
    scale_path_for(day, filial).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    clear_board_cache(day, filial)
    return data


def scale_lookup(day: str, filial: str) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id_tecnico") or ""): item
        for item in load_scale(day, filial).get("items", [])
        if str(item.get("id_tecnico") or "")
    }


def default_scale_info(tech_id: str = "", tech_name: str = "") -> dict[str, Any]:
    return {
        "id_tecnico": str(tech_id or ""),
        "tecnico": tech_name,
        "tecnico_curto": short_tech_name(tech_name),
        "situacao": "Sem escala",
        "funcao": "",
        "dupla_id": "",
        "dupla_nome": "",
        "parceiro_ids": [],
        "observacao": "",
        "fora_da_escala": True,
    }


def normalize_scale_info(item: dict[str, Any] | None, tech_id: str = "", tech_name: str = "") -> dict[str, Any]:
    if not item:
        return default_scale_info(tech_id, tech_name)
    data = {
        **default_scale_info(tech_id or str(item.get("id_tecnico") or ""), tech_name or str(item.get("tecnico") or "")),
        **item,
    }
    situacao = str(data.get("situacao") or "").strip() or "Sem escala"
    data["situacao"] = situacao
    if situacao.lower() in {"sem escala", "folga", "ausente", "atestado"}:
        data["funcao"] = "Sem escala"
    data["fora_da_escala"] = situacao.lower() in {"sem escala", "folga", "ausente", "atestado"}
    data["parceiro_ids"] = [str(value) for value in data.get("parceiro_ids") or []]
    return data


def clear_board_cache(day: str, filial: str) -> None:
    for key in list(_CACHE):
        if key.startswith(f"{day}|{filial}|"):
            _CACHE.pop(key, None)
    for include_names in (False, True):
        path = snapshot_path_for(day, filial, include_names)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def scale_summary(scale: dict[str, Any]) -> dict[str, Any]:
    items = scale.get("items") or []
    situacoes = Counter(str(item.get("situacao") or "Sem escala") for item in items)
    funcoes = Counter(str(item.get("funcao") or "Sem funcao") for item in items)
    duplas = Counter(str(item.get("dupla_id") or "") for item in items if item.get("dupla_id"))
    return {
        "total_escalados": len(items),
        "situacoes": dict(situacoes),
        "funcoes": dict(funcoes),
        "duplas": len(duplas),
        "updated_at": scale.get("updated_at") or "",
    }


def snapshot_age_seconds(path: Path) -> int | None:
    if not path.exists():
        return None
    return round(time.time() - path.stat().st_mtime)


def with_cache_metadata(payload: dict[str, Any], source: str, age_seconds: int | None, started_at: float) -> dict[str, Any]:
    data = dict(payload)
    extras = load_os_extra()
    for column in data.get("columns") or []:
        for item in column.get("items") or []:
            os_id = str(item.get("id") or "")
            extra = extras.get(os_id, {}) if os_id else {}
            if isinstance(extra, dict):
                item["extra"] = extra
                if item.get("fibra_utilizada_m") is None:
                    item["fibra_utilizada_m"] = extra.get("fibra_utilizada_m")
                    item["fibra_source"] = "Registro local" if item.get("fibra_utilizada_m") is not None else ""
    data["source"] = source
    data["cache"] = {
        "hit": source != "ixc_live",
        "source": source,
        "age_seconds": age_seconds,
        "ttl_seconds": CACHE_SECONDS,
    }
    data["performance"] = {
        **data.get("performance", {}),
        "served_from_cache_ms": round((time.perf_counter() - started_at) * 1000),
        "cache_ttl_seconds": CACHE_SECONDS,
    }
    return data


def read_snapshot(day: str, filial: str, include_client_names: bool, started_at: float) -> dict[str, Any] | None:
    path = snapshot_path_for(day, filial, include_client_names)
    age_seconds = snapshot_age_seconds(path)
    if age_seconds is None or age_seconds >= CACHE_SECONDS:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("snapshot_version") != SNAPSHOT_VERSION:
        return None
    return with_cache_metadata(payload, "snapshot", age_seconds, started_at)


def write_snapshot(day: str, filial: str, include_client_names: bool, payload: dict[str, Any]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = snapshot_path_for(day, filial, include_client_names)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=True, default=str), encoding="utf-8")
    temp_path.replace(path)


def post_ixc_action(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"{monitor.BASE_URL}/{endpoint}",
        headers=write_headers(),
        json=payload,
        timeout=45,
    )
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:1000]
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "IXC recusou a escrita.",
                "status_code": response.status_code,
                "response": body,
            },
        )
    if isinstance(body, dict) and str(body.get("type") or "").lower() == "error":
        raise HTTPException(
            status_code=502,
            detail={
                "message": body.get("message") or "IXC retornou erro no corpo da resposta.",
                "status_code": response.status_code,
                "response": body,
            },
        )
    return {"status_code": response.status_code, "response": body}


def listar_ixc_tudo(
    table: str,
    qtype: str,
    query: Any,
    oper: str = "=",
    sortname: str | None = None,
    sortorder: str = "asc",
    rp: int = 500,
    grid_params: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    page = 1
    while True:
        body = {
            "qtype": qtype,
            "query": str(query),
            "oper": oper,
            "page": str(page),
            "rp": str(rp),
            "sortname": sortname or qtype,
            "sortorder": sortorder,
        }
        if grid_params:
            body["grid_param"] = json.dumps(grid_params, ensure_ascii=True)
        response = requests.post(
            f"{monitor.BASE_URL}/{table}",
            headers=monitor.headers(),
            json=body,
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        regs = data.get("registros") or []
        rows.extend(regs)
        total = int(data.get("total") or 0)
        if len(rows) >= total or not regs:
            return rows
        page += 1


def fetch_ixc_os(os_id: str) -> dict[str, Any] | None:
    rows = monitor.listar_tudo(
        "su_oss_chamado",
        "su_oss_chamado.id",
        os_id,
        "=",
        "su_oss_chamado.id",
        "asc",
        rp=1,
    )
    return rows[0] if rows else None


def fetch_client_info(client_id: str) -> dict[str, str]:
    client_id = str(client_id or "")
    cached = _CLIENT_CACHE.get(client_id)
    if cached and time.time() - cached[0] < MAP_CACHE_SECONDS:
        return cached[1]

    rows = monitor.listar(
        "cliente",
        "cliente.id",
        client_id,
        "=",
        rp=1,
        sortname="cliente.id",
    ).get("registros") or []
    row = rows[0] if rows else {}
    info = {
        "id_cliente": client_id,
        "cliente": row.get("razao") or row.get("fantasia") or f"Cliente #{client_id}",
    }
    _CLIENT_CACHE[client_id] = (time.time(), info)
    return info


def fetch_client_names_fast(client_ids: Any, max_workers: int = 8) -> dict[str, str]:
    ids = sorted({str(value) for value in client_ids if value})
    if not ids:
        return {}

    names: dict[str, str] = {}
    missing: list[str] = []
    for client_id in ids:
        cached = _CLIENT_CACHE.get(client_id)
        if cached and time.time() - cached[0] < MAP_CACHE_SECONDS:
            names[client_id] = cached[1]["cliente"]
        else:
            missing.append(client_id)

    if missing:
        workers = max(1, min(max_workers, len(missing)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_client_info, client_id): client_id for client_id in missing}
            for future in as_completed(futures):
                client_id = futures[future]
                try:
                    names[client_id] = future.result()["cliente"]
                except Exception:  # noqa: BLE001
                    names[client_id] = f"Cliente #{client_id}"
    return names


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("m", "").replace("M", "").strip()
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def normalized_contains_fibra(value: Any) -> bool:
    text = monitor.normalize_text(value)
    return "fibra" in text or "drop" in text


def fetch_product_info(product_id: Any) -> dict[str, Any]:
    key = str(product_id or "").strip()
    if not key:
        return {}
    cached = _PRODUCT_CACHE.get(key)
    if cached and time.time() - cached[0] < MAP_CACHE_SECONDS:
        return cached[1]

    rows = monitor.listar(
        "produtos",
        "produtos.id",
        key,
        "=",
        rp=1,
        sortname="produtos.id",
    ).get("registros") or []
    info = rows[0] if rows else {}
    _PRODUCT_CACHE[key] = (time.time(), info)
    return info


def product_name_for(row: dict[str, Any]) -> str:
    direct_fields = (
        "produto",
        "descricao",
        "descricao_produto",
        "nome_produto",
        "produto_descricao",
        "id_produto_label",
    )
    for field in direct_fields:
        if row.get(field):
            return str(row.get(field) or "")

    product_id = row.get("id_produto") or row.get("produto_id")
    try:
        product = fetch_product_info(product_id)
    except Exception:  # noqa: BLE001
        product = {}
    for field in ("descricao", "produto", "nome"):
        if product.get(field):
            return str(product.get(field) or "")
    return ""


def quantity_for_product_row(row: dict[str, Any]) -> float | None:
    for field in ("quantidade", "qtde", "qtd", "metragem", "metros", "comprimento", "qtde_saida"):
        number = parse_number(row.get(field))
        if number is not None:
            return number
    return None


def fetch_os_products(os_id: str, force_refresh: bool = False) -> list[dict[str, Any]]:
    global _OS_PRODUCT_QTYPE
    key = str(os_id or "").strip()
    if not key:
        return []
    cached = _OS_PRODUCTS_CACHE.get(key)
    if cached and not force_refresh and time.time() - cached[0] < OS_PRODUCT_CACHE_SECONDS:
        return cached[1]

    qtypes = [_OS_PRODUCT_QTYPE] if _OS_PRODUCT_QTYPE else []
    qtypes.extend(qtype for qtype in OS_PRODUCT_QTYPE_CANDIDATES if qtype not in qtypes)
    last_error: Exception | None = None
    for qtype in qtypes:
        if not qtype:
            continue
        try:
            rows = monitor.listar_tudo(
                "su_oss_mov_produto",
                qtype,
                key,
                "=",
                "movimento_produtos.id",
                "asc",
                rp=100,
            )
            _OS_PRODUCT_QTYPE = qtype
            _OS_PRODUCTS_CACHE[key] = (time.time(), rows)
            return rows
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    if last_error:
        raise last_error
    return []


def fiber_usage_from_products(products: list[dict[str, Any]]) -> dict[str, Any]:
    matches = []
    total = 0.0
    has_quantity = False
    for row in products:
        name = product_name_for(row)
        if not normalized_contains_fibra(name):
            continue
        quantity = quantity_for_product_row(row)
        if quantity is not None:
            total += quantity
            has_quantity = True
        matches.append(
            {
                "id": str(row.get("id") or ""),
                "id_produto": str(row.get("id_produto") or row.get("produto_id") or ""),
                "produto": name or "Fibra/DROP",
                "quantidade": quantity,
            }
        )

    if not matches:
        return {"fibra_utilizada_m": None, "fibra_source": "", "produtos_fibra": []}

    value: int | float | None = round(total, 2) if has_quantity else None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return {
        "fibra_utilizada_m": value,
        "fibra_source": "IXC: aba Produto",
        "produtos_fibra": matches,
    }


def fetch_fiber_usage_for_os(os_id: str, force_refresh: bool = False) -> dict[str, Any]:
    try:
        return fiber_usage_from_products(fetch_os_products(os_id, force_refresh=force_refresh))
    except Exception as exc:  # noqa: BLE001
        return {
            "fibra_utilizada_m": None,
            "fibra_source": "",
            "produtos_fibra": [],
            "fibra_error": str(exc),
        }


def fetch_os_products_bulk(os_ids: Any, force_refresh: bool = False) -> dict[str, list[dict[str, Any]]]:
    ids = sorted({str(value) for value in os_ids if value}, key=lambda value: int(value) if value.isdigit() else 0)
    numeric_ids = [int(value) for value in ids if value.isdigit()]
    if not numeric_ids:
        return {}
    if force_refresh:
        for os_id in ids:
            _OS_PRODUCTS_CACHE.pop(os_id, None)

    min_id = min(numeric_ids)
    max_id = max(numeric_ids)
    rows = listar_ixc_tudo(
        "su_oss_mov_produto",
        "movimento_produtos.id_oss_chamado",
        min_id,
        ">=",
        "movimento_produtos.id",
        "asc",
        rp=1000,
        grid_params=[
            {"TB": "movimento_produtos.id_oss_chamado", "OP": "<=", "P": str(max_id)},
            {"TB": "movimento_produtos.id_produto", "OP": ">=", "P": "1"},
        ],
    )
    wanted = set(ids)
    grouped: dict[str, list[dict[str, Any]]] = {os_id: [] for os_id in ids}
    for row in rows:
        os_id = str(row.get("id_oss_chamado") or row.get("id_chamado") or row.get("id_os") or "")
        if os_id in wanted:
            grouped.setdefault(os_id, []).append(row)
            _OS_PRODUCTS_CACHE[os_id] = (time.time(), grouped[os_id])
    return grouped


def fetch_fiber_usage_map(os_ids: Any, max_workers: int = 8, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    ids = sorted({str(value) for value in os_ids if value})
    if not ids:
        return {}
    try:
        product_map = fetch_os_products_bulk(ids, force_refresh=force_refresh)
        return {os_id: fiber_usage_from_products(product_map.get(os_id, [])) for os_id in ids}
    except Exception:
        pass

    results: dict[str, dict[str, Any]] = {}
    workers = max(1, min(max_workers, len(ids)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_fiber_usage_for_os, os_id, force_refresh): os_id for os_id in ids}
        for future in as_completed(futures):
            os_id = futures[future]
            try:
                results[os_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[os_id] = {
                    "fibra_utilizada_m": None,
                    "fibra_source": "",
                    "produtos_fibra": [],
                    "fibra_error": str(exc),
                }
    return results


def same_ixc_minute(actual: Any, expected: str) -> bool:
    if not actual or not expected:
        return False
    actual_text = str(actual)
    expected_text = str(expected)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            expected_dt = datetime.strptime(expected_text, fmt)
            return actual_text[:16] == expected_dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return actual_text[:16] == expected_text[:16]


def ixc_action_datetime(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M:%S") if value else ""


def verify_ixc_action(
    os_id: str,
    action_key: str,
    payload: dict[str, Any],
    tech_change: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = fetch_ixc_os(os_id)
    if not row:
        return {"ok": False, "message": "Nao consegui reler a OS no IXC depois da escrita.", "row": None}

    checks: dict[str, bool] = {}
    if action_key == "agendar" and tech_change and (
        tech_change.get("is_change") or tech_change.get("is_status_change")
    ):
        checks["status"] = str(row.get("status") or "").upper() == str(payload.get("status") or "").upper()
        checks["id_tecnico"] = str(row.get("id_tecnico") or "") == str(payload.get("id_tecnico") or "")
    elif action_key == "agendar":
        checks["data_agenda"] = same_ixc_minute(row.get("data_agenda"), payload.get("data_agendamento", ""))
        checks["data_agenda_final"] = same_ixc_minute(
            row.get("data_agenda_final"), payload.get("data_agendamento_final", "")
        )
        if payload.get("id_tecnico"):
            checks["id_tecnico"] = str(row.get("id_tecnico") or "") == str(payload.get("id_tecnico"))
    elif payload.get("status"):
        checks["status"] = str(row.get("status") or "").upper() == str(payload.get("status") or "").upper()

    return {
        "ok": all(checks.values()) if checks else True,
        "checks": checks,
        "message": "IXC relido apos escrita.",
        "row": {
            "id": str(row.get("id") or ""),
            "status": row.get("status") or "",
            "id_tecnico": str(row.get("id_tecnico") or ""),
            "data_agenda": row.get("data_agenda") or "",
            "data_agenda_final": row.get("data_agenda_final") or "",
            "data_inicio": row.get("data_inicio") or "",
            "data_fechamento": row.get("data_fechamento") or "",
            "ultima_atualizacao": row.get("ultima_atualizacao") or "",
        },
    }


def technician_change_info(os_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    target_id = str(payload.get("id_tecnico") or "")
    target_status = str(payload.get("status") or "").upper()
    if not target_id:
        return {
            "is_change": False,
            "is_status_change": False,
            "current_id": "",
            "target_id": target_id,
            "current_status": "",
            "target_status": target_status,
        }
    row = fetch_ixc_os(os_id)
    current_id = str((row or {}).get("id_tecnico") or "")
    current_status = str((row or {}).get("status") or "").upper()
    return {
        "is_change": bool(current_id and target_id and current_id != target_id),
        "is_status_change": bool(current_status and target_status and current_status != target_status),
        "current_id": current_id,
        "target_id": target_id,
        "current_status": current_status,
        "target_status": target_status,
    }


def has_real_value(value: Any) -> bool:
    return bool(value) and not str(value).startswith("0000")


def status_label(status: Any) -> str:
    text = str(status or "").upper()
    return STATUS_LABELS.get(text, text or "Pendente")


def state_for(row: dict[str, Any]) -> dict[str, str]:
    status = str(row.get("status") or "").upper()
    if status == "F":
        return {"key": "done", "label": "Finalizada"}
    if status in {"EX", "EP"}:
        return {"key": "running", "label": status_label(status)}
    if status in {"AG", "RAG", "AN", "EN", "A"}:
        return {"key": "waiting", "label": status_label(status)}
    return {"key": "waiting", "label": status_label(status)}


def agenda_alert_for(row: dict[str, Any]) -> dict[str, Any]:
    compliance = row.get("cumprimento_agenda") or {}
    compliance_status = str(compliance.get("status") or "").lower()
    level = str(compliance.get("nivel") or "").lower()
    is_late = "nao cumprido" in compliance_status or level == "critico"
    return {
        "is_late": is_late,
        "status": compliance.get("status") or "",
        "motivo": compliance.get("motivo") or "",
        "nivel": compliance.get("nivel") or "",
    }


def timeline_for(row: dict[str, Any]) -> list[dict[str, str]]:
    status = str(row.get("status") or "").upper()
    items = [
        {
            "label": "Abertura",
            "value": row.get("data_abertura") or "",
            "note": "OS criada no IXC",
            "kind": "done",
        },
        {
            "label": "Agenda",
            "value": row.get("data_agenda") or "",
            "note": row.get("agenda_descricao") or row.get("melhor_horario_agenda") or "Horario agendado",
            "kind": "waiting",
        },
    ]

    if has_real_value(row.get("data_agenda_final")):
        items.append(
            {
                "label": "Fim da janela",
                "value": row.get("data_agenda_final") or "",
                "note": "Limite da janela agendada",
                "kind": "waiting",
            }
        )

    if has_real_value(row.get("data_inicio")) or has_real_value(row.get("data_hora_execucao")):
        note = "Inicio/execucao registrado no IXC"
        kind = "done" if status in {"EX", "EP", "F"} else "neutral"
        if status == "AG":
            note = "IXC trouxe horario de inicio, mas o status oficial ainda esta AG"
        items.append(
            {
                "label": "Inicio",
                "value": row.get("data_inicio") or row.get("data_hora_execucao") or "",
                "note": note,
                "kind": kind,
            }
        )

    if has_real_value(row.get("data_prazo_limite")):
        items.append(
            {
                "label": "Prazo SLA",
                "value": row.get("data_prazo_limite") or "",
                "note": f"SLA IXC: {row.get('status_sla') or 'sem status'}",
                "kind": "waiting",
            }
        )

    if has_real_value(row.get("data_fechamento")):
        items.append(
            {
                "label": "Fechamento",
                "value": row.get("data_fechamento") or "",
                "note": "OS finalizada",
                "kind": "done",
            }
        )

    compliance = row.get("cumprimento_agenda") or {}
    if compliance:
        items.append(
            {
                "label": "Leitura da agenda",
                "value": "",
                "note": compliance.get("motivo") or compliance.get("status") or "sem leitura",
                "kind": "alert" if compliance.get("nivel") == "critico" else "neutral",
            }
        )
    return items


def action_timeline_for(os_id: str) -> list[dict[str, str]]:
    if not ACTION_LOG_PATH.exists():
        return []
    events: list[dict[str, str]] = []
    try:
        lines = ACTION_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-100:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload") or {}
        if str(payload.get("id_chamado") or "") != str(os_id):
            continue
        error = record.get("error") or {}
        error_response = error.get("response") if isinstance(error, dict) else {}
        response = record.get("ixc_response") or error_response or {}
        response_id = response.get("id") if isinstance(response, dict) else ""
        status = response.get("type") if isinstance(response, dict) else ""
        target = str(payload.get("id_tecnico") or "--")
        note = f"{record.get('acao') or 'acao'} para tecnico ID {target}"
        if response_id:
            note = f"{note}; evento IXC {response_id}"
        if status:
            note = f"{note}; retorno {status}"
        events.append(
            {
                "label": "Acao Kanban",
                "value": str(record.get("created_at") or ""),
                "note": note,
                "kind": "done" if record.get("ok") else "alert",
            }
        )
    return events[-10:]


def sanitize_row(
    row: dict[str, Any],
    include_client_names: bool = False,
    fiber_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client_id = str(row.get("id_cliente") or "")
    os_id = str(row.get("id") or "")
    extra = extra_for_os(os_id)
    fiber_info = fiber_info or {}
    fiber_value = fiber_info.get("fibra_utilizada_m")
    fiber_source = fiber_info.get("fibra_source") or ""
    if fiber_value is None:
        fiber_value = extra.get("fibra_utilizada_m")
        fiber_source = "Registro local" if fiber_value is not None else ""
    item = {
        "id": os_id,
        "id_cliente": client_id,
        "cliente": row.get("cliente") if include_client_names else (f"Cliente #{client_id}" if client_id else "Cliente"),
        "filial": str(row.get("filial") or ""),
        "filial_nome": row.get("filial_nome") or "",
        "status": row.get("status") or "",
        "status_label": status_label(row.get("status")),
        "status_source": "IXC: su_oss_chamado.status",
        "state": state_for(row),
        "agenda_alert": agenda_alert_for(row),
        "id_tecnico": str(row.get("id_tecnico") or ""),
        "tecnico": row.get("tecnico") or "Sem tecnico",
        "id_assunto": str(row.get("id_assunto") or ""),
        "assunto": row.get("assunto") or "",
        "data_abertura": row.get("data_abertura") or "",
        "data_agenda": row.get("data_agenda") or "",
        "data_agenda_final": row.get("data_agenda_final") or "",
        "melhor_horario_agenda": row.get("melhor_horario_agenda") or "",
        "agenda_descricao": row.get("agenda_descricao") or "",
        "data_inicio": row.get("data_inicio") or "",
        "data_hora_execucao": row.get("data_hora_execucao") or "",
        "data_fechamento": row.get("data_fechamento") or "",
        "status_sla": row.get("status_sla") or "",
        "data_prazo_limite": row.get("data_prazo_limite") or "",
        "cumprimento_agenda": row.get("cumprimento_agenda") or {},
        "agendamento_descricao": row.get("agendamento_descricao") or {},
        "fibra_utilizada_m": fiber_value,
        "fibra_source": fiber_source,
        "produtos_fibra": fiber_info.get("produtos_fibra") or [],
        "fibra_error": fiber_info.get("fibra_error") or "",
        "extra": extra,
        "timeline": timeline_for(row) + action_timeline_for(os_id),
    }
    return item


def short_tech_name(value: str) -> str:
    parts = [part for part in str(value or "").split() if part]
    if not parts:
        return "Sem tecnico"
    if len(parts) == 1:
        return parts[0].title()
    return f"{parts[0].title()} {parts[-1].title()}"


def short_tech_name_for_id(tech_id: str, name: str) -> str:
    return TECHNICIAN_SHORT_NAME_OVERRIDES.get(str(tech_id), short_tech_name(name))


def load_filial_technicians(filial: str, force_refresh: bool = False) -> list[dict[str, Any]]:
    cached = _TECHNICIAN_CACHE.get(str(filial))
    if cached and not force_refresh and time.time() - cached[0] < MAP_CACHE_SECONDS:
        return cached[1]

    rows = monitor.listar_tudo(
        "funcionarios",
        "funcionarios.id",
        "0",
        ">",
        "funcionarios.funcionario",
        "asc",
        rp=1000,
    )
    technicians = []
    seen: set[str] = set()
    for row in rows:
        tech_id = str(row.get("id") or "")
        if not tech_id or tech_id in seen:
            continue
        if str(row.get("filial_id") or "") != str(filial):
            continue
        if tech_id in EXCLUDED_TECHNICIAN_IDS_BY_FILIAL.get(str(filial), set()):
            continue
        if str(row.get("ativo") or "").upper() != "S":
            continue
        if str(row.get("mostrar_no_quadro_kanban") or "").upper() != "S":
            continue
        is_technician_function = str(row.get("id_funcao") or "") in TECHNICIAN_FUNCTION_IDS
        is_extra_technician = tech_id in EXTRA_TECHNICIAN_IDS_BY_FILIAL.get(str(filial), set())
        if not is_technician_function and not is_extra_technician:
            continue
        name = str(row.get("funcionario") or "").strip()
        if not name:
            continue
        seen.add(tech_id)
        technicians.append(
            {
                "id": tech_id,
                "tecnico": name,
                "tecnico_curto": short_tech_name_for_id(tech_id, name),
                "filial": str(filial),
                "id_funcao": str(row.get("id_funcao") or ""),
            }
        )

    technicians.sort(key=lambda item: (item["tecnico_curto"].casefold(), item["tecnico"].casefold(), item["id"]))
    _TECHNICIAN_CACHE[str(filial)] = (time.time(), technicians)
    return technicians


def build_payload(day: str, filial: str, include_client_names: bool, force_refresh: bool = False) -> dict[str, Any]:
    started_at = time.perf_counter()
    cache_key = cache_key_for(day, filial, include_client_names)
    if not force_refresh:
        cached = _CACHE.get(cache_key)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            age_seconds = round(time.time() - cached[0])
            return with_cache_metadata(cached[1], "memory", age_seconds, started_at)
        snapshot = read_snapshot(day, filial, include_client_names, started_at)
        if snapshot:
            _CACHE[cache_key] = (time.time() - int(snapshot["cache"]["age_seconds"]), snapshot)
            return snapshot

    reference = current_reference(day)
    timings: dict[str, int] = {}
    mark = time.perf_counter()
    funcionarios, assuntos = load_reference_maps()
    timings["mapas_ms"] = round((time.perf_counter() - mark) * 1000)
    scale = load_scale(day, filial)
    scale_by_id = scale_lookup(day, filial)
    roster_by_id = {str(item.get("id") or ""): item for item in load_filial_technicians(filial)}
    mark = time.perf_counter()
    agenda = monitor.fetch_agenda(day)
    timings["agenda_ixc_ms"] = round((time.perf_counter() - mark) * 1000)
    agenda_filial = [row for row in agenda if str(row.get("id_filial") or "") == str(filial)]
    mark = time.perf_counter()
    clientes = (
        fetch_client_names_fast((row.get("id_cliente") for row in agenda_filial), max_workers=8)
        if include_client_names
        else {}
    )
    timings["clientes_ixc_ms"] = round((time.perf_counter() - mark) * 1000)
    mark = time.perf_counter()
    compact = monitor.add_row_start_intervals(
        [monitor.compact_row(row, funcionarios, assuntos, clientes, reference) for row in agenda_filial]
    )
    filtered = compact
    filtered.sort(key=lambda row: (row.get("tecnico") or "", row.get("data_agenda") or "", row.get("id") or ""))
    timings["processamento_ms"] = round((time.perf_counter() - mark) * 1000)

    mark = time.perf_counter()
    fiber_by_os = fetch_fiber_usage_map((row.get("id") for row in filtered), max_workers=8, force_refresh=force_refresh)
    timings["produtos_fibra_ixc_ms"] = round((time.perf_counter() - mark) * 1000)

    os_items = [sanitize_row(row, include_client_names, fiber_by_os.get(str(row.get("id") or ""))) for row in filtered]
    for item in os_items:
        tech_id = str(item.get("id_tecnico") or "")
        item["escala"] = normalize_scale_info(scale_by_id.get(tech_id), tech_id, item.get("tecnico") or "")
    status_counts = Counter(row["status"] or "sem_status" for row in os_items)
    state_counts = Counter(row["state"]["key"] for row in os_items)
    late_count = sum(1 for row in os_items if row.get("agenda_alert", {}).get("is_late"))
    by_tech: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in os_items:
        by_tech[item["tecnico"]].append(item)

    columns = []
    for tech, items in sorted(by_tech.items(), key=lambda pair: (-len(pair[1]), short_tech_name(pair[0]))):
        counts = Counter(item["state"]["key"] for item in items)
        tech_id = str(items[0].get("id_tecnico") or "")
        escala = normalize_scale_info(scale_by_id.get(tech_id), tech_id, tech)
        columns.append(
            {
                "tecnico": tech,
                "tecnico_curto": short_tech_name(tech),
                "id_tecnico": tech_id,
                "escala": escala,
                "total": len(items),
                "counts": dict(counts),
                "late_count": sum(1 for item in items if item.get("agenda_alert", {}).get("is_late")),
                "items": items,
            }
        )
    visible_empty_status = {"Em campo"}
    existing_ids = {str(column.get("id_tecnico") or "") for column in columns}
    for tech_id, item in scale_by_id.items():
        if tech_id in existing_ids:
            continue
        escala = normalize_scale_info(item, tech_id, item.get("tecnico") or "")
        if escala.get("situacao") not in visible_empty_status:
            continue
        roster = roster_by_id.get(tech_id, {})
        tech_name = escala.get("tecnico") or roster.get("tecnico") or f"Tecnico {tech_id}"
        columns.append(
            {
                "tecnico": tech_name,
                "tecnico_curto": escala.get("tecnico_curto") or roster.get("tecnico_curto") or short_tech_name(tech_name),
                "id_tecnico": tech_id,
                "escala": normalize_scale_info(escala, tech_id, tech_name),
                "total": 0,
                "counts": {},
                "late_count": 0,
                "items": [],
            }
        )
    columns.sort(key=lambda column: (-int(column.get("total") or 0), short_tech_name(column.get("tecnico") or "")))

    payload = {
        "source": "ixc_live",
        "snapshot_version": SNAPSHOT_VERSION,
        "created_at": datetime.now(monitor.TZ).isoformat(timespec="seconds"),
        "date": day,
        "filial": filial,
        "filial_nome": APP_FILIAIS.get(str(filial), f"Filial {filial}"),
        "reference_time": reference.isoformat(timespec="seconds"),
        "summary": {
            "total": len(os_items),
            "tecnicos": len(columns),
            "status": dict(status_counts),
            "states": {**dict(state_counts), "late": late_count},
            "scale": scale_summary(scale),
        },
        "performance": {
            "consulta_total_ms": round((time.perf_counter() - started_at) * 1000),
            "timings": timings,
            "cache_ttl_seconds": CACHE_SECONDS,
            "map_cache_ttl_seconds": MAP_CACHE_SECONDS,
            "client_names": include_client_names,
        },
        "columns": columns,
        "scale": scale,
        "cache": {"hit": False, "ttl_seconds": CACHE_SECONDS},
    }
    _CACHE[cache_key] = (time.time(), payload)
    write_snapshot(day, filial, include_client_names, payload)
    return payload


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    return {"ok": True, "service": "kanban-os-tecnicos", "mode": "direct-write-ui"}


@app.post("/api/login")
def login(request: LoginRequest):
    username = request.username.strip().lower()
    user = users().get(username)
    if not user or user["password_hash"] != password_hash(request.password):
        append_audit("login_failed", details={"username": username})
        raise HTTPException(status_code=401, detail="Usuario ou senha invalidos.")
    token = secrets.token_urlsafe(32)
    session = {
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "created_ts": time.time(),
        "last_seen_ts": time.time(),
    }
    _SESSIONS[token] = session
    append_audit("login_success", actor=session)
    return {"ok": True, "token": token, "user": public_user(session), "expires_in_seconds": SESSION_SECONDS}


@app.get("/api/me")
def me(session: dict[str, Any] = Depends(require_session)):
    return {"ok": True, "user": public_user(session)}


@app.post("/api/logout")
def logout(request: LogoutRequest, session: dict[str, Any] = Depends(require_session)):
    if request.token:
        _SESSIONS.pop(request.token, None)
    append_audit("logout", actor=session)
    return {"ok": True}


@app.get("/api/audit")
def audit(limit: int = Query(default=100, ge=1, le=500), session: dict[str, Any] = Depends(require_admin)):
    append_audit("audit_viewed", actor=session, details={"limit": limit})
    return {"ok": True, "items": read_audit(limit)}


@app.get("/api/users")
def list_users(session: dict[str, Any] = Depends(require_admin)):
    append_audit("users_viewed", actor=session)
    return {"ok": True, "items": [public_user_record(user) for user in users().values()]}


@app.post("/api/users")
def create_user(payload: UserCreateRequest, session: dict[str, Any] = Depends(require_admin)):
    created = create_local_user(payload, session)
    append_audit(
        "user_created",
        actor=session,
        details={"username": created["username"], "role": created["role"]},
    )
    return {"ok": True, "user": created}


@app.get("/api/filiais")
def filiais(session: dict[str, Any] = Depends(require_session)):
    return [{"id": key, "nome": value} for key, value in APP_FILIAIS.items()]


@app.get("/api/tecnicos")
def tecnicos(
    filial: str = Query(default="1", description="ID da filial IXC"),
    refresh: bool = Query(default=False, description="Ignora cache e consulta funcionarios agora."),
    session: dict[str, Any] = Depends(require_session),
):
    if filial not in APP_FILIAIS:
        raise HTTPException(status_code=400, detail="Filial nao habilitada neste MVP.")
    try:
        technicians = load_filial_technicians(filial, force_refresh=refresh)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="Token IXC local nao encontrado.") from exc
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": "Falha ao consultar tecnicos no IXC.",
                "detail": str(exc),
            },
        )
    return {
        "filial": filial,
        "filial_nome": APP_FILIAIS.get(str(filial), f"Filial {filial}"),
        "total": len(technicians),
        "items": technicians,
    }


@app.get("/api/scale")
def get_scale(
    day: str | None = Query(default=None, description="Data YYYY-MM-DD"),
    filial: str = Query(default="1", description="ID da filial IXC"),
    session: dict[str, Any] = Depends(require_session),
):
    parsed_day = parse_day(day)
    if filial not in APP_FILIAIS:
        raise HTTPException(status_code=400, detail="Filial nao habilitada neste MVP.")
    return load_scale(parsed_day, filial)


@app.put("/api/scale")
def put_scale(payload: ScalePayload, session: dict[str, Any] = Depends(require_session)):
    parsed_day = parse_day(payload.date)
    if payload.filial not in APP_FILIAIS:
        raise HTTPException(status_code=400, detail="Filial nao habilitada neste MVP.")
    normalized = ScalePayload(date=parsed_day, filial=str(payload.filial), items=payload.items)
    saved = save_scale(normalized)
    append_audit(
        "scale_saved",
        actor=session,
        details={"date": parsed_day, "filial": str(payload.filial), "items": len(payload.items)},
    )
    return saved


@app.get("/api/os")
def os_board(
    day: str | None = Query(default=None, description="Data YYYY-MM-DD"),
    filial: str = Query(default="1", description="ID da filial IXC"),
    include_client_names: bool = Query(default=False, description="Exibe nomes de clientes; falso mascara os dados."),
    refresh: bool = Query(default=False, description="Ignora cache/snapshot e consulta IXC agora."),
    session: dict[str, Any] = Depends(require_session),
):
    parsed_day = parse_day(day)
    if filial not in APP_FILIAIS:
        raise HTTPException(status_code=400, detail="Filial nao habilitada neste MVP.")
    try:
        return build_payload(parsed_day, filial, include_client_names, force_refresh=refresh)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="Token IXC local nao encontrado.") from exc
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": "Falha ao consultar IXC.",
                "detail": str(exc),
            },
        )


@app.get("/api/os/{os_id}/cliente")
def os_cliente(os_id: str, session: dict[str, Any] = Depends(require_session)):
    try:
        row = fetch_ixc_os(os_id)
        if not row:
            raise HTTPException(status_code=404, detail="OS nao encontrada no IXC.")
        client_id = str(row.get("id_cliente") or "")
        if not client_id:
            return {"id_os": os_id, "id_cliente": "", "cliente": "Cliente"}
        return {"id_os": os_id, **fetch_client_info(client_id)}
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="Token IXC local nao encontrado.") from exc
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": "Falha ao consultar cliente da OS no IXC.",
                "detail": str(exc),
            },
        )


@app.get("/api/os/{os_id}/extra")
def get_os_extra(os_id: str, session: dict[str, Any] = Depends(require_session)):
    fiber_info = fetch_fiber_usage_for_os(str(os_id))
    extra = extra_for_os(os_id)
    fiber_value = fiber_info.get("fibra_utilizada_m")
    fiber_source = fiber_info.get("fibra_source") or ""
    if fiber_value is None:
        fiber_value = extra.get("fibra_utilizada_m")
        fiber_source = "Registro local" if fiber_value is not None else ""
    return {
        "id_os": str(os_id),
        "extra": extra,
        "fibra_utilizada_m": fiber_value,
        "fibra_source": fiber_source,
        "produtos_fibra": fiber_info.get("produtos_fibra") or [],
        "fibra_error": fiber_info.get("fibra_error") or "",
    }


@app.put("/api/os/{os_id}/extra")
def put_os_extra(os_id: str, payload: OsExtraPayload, session: dict[str, Any] = Depends(require_session)):
    saved = save_os_extra(os_id, payload, session)
    append_audit(
        "os_extra_saved",
        actor=session,
        details={"os_id": str(os_id), "fibra_utilizada_m": payload.fibra_utilizada_m},
    )
    return saved


@app.post("/api/os/{os_id}/reagendar")
def dry_run_reagendar(os_id: str, request: ScheduleRequest, session: dict[str, Any] = Depends(require_session)):
    if request.status not in {"AG", "RAG"}:
        raise HTTPException(status_code=400, detail="Status de agendamento deve ser AG ou RAG.")
    try:
        start = datetime.strptime(request.data_agendamento, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="data_agendamento invalida. Use YYYY-MM-DD HH:MM:SS.") from exc

    end_text = request.data_agendamento_final or request.data_agendamento
    try:
        end = datetime.strptime(end_text, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="data_agendamento_final invalida. Use YYYY-MM-DD HH:MM:SS.") from exc
    if end < start:
        raise HTTPException(status_code=400, detail="Fim da agenda nao pode ser antes do inicio.")

    payload = {
        "id_chamado": os_id,
        "data_agendamento": request.data_agendamento,
        "data_agendamento_final": end_text,
        "id_resposta": "",
        "mensagem": request.mensagem or "Agendamento solicitado pelo Kanban OS Tecnicos VIP.",
        "id_tecnico": request.id_tecnico or "",
        "id_equipe": "",
        "status": request.status,
        "data": "",
        "id_evento": "",
        "id_compromisso": "",
        "latitude": "",
        "longitude": "",
        "gps_time": "",
    }
    return {
        "ok": True,
        "mode": "dry-run",
        "message": "Payload validado. Escrita no IXC permanece desativada.",
        "endpoint_ixc": "su_oss_chamado_reagendar",
        "required_base": ["id_chamado", "data_agendamento", "data_agendamento_final", "status"],
        "payload": payload,
    }


def validate_ixc_datetime(value: str | None, field: str, required: bool = False) -> str:
    if not value:
        if required:
            raise HTTPException(status_code=400, detail=f"{field} e obrigatorio para esta acao.")
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} invalido. Use YYYY-MM-DD HH:MM:SS.") from exc
    return value


def build_action_payload(os_id: str, request: OsActionRequest) -> tuple[str, dict[str, Any], dict[str, Any]]:
    action_key = request.acao.strip().lower()
    action = ACTION_DEFS.get(action_key)
    if not action:
        raise HTTPException(status_code=400, detail="Acao nao suportada neste MVP.")

    needs_schedule = action_key == "agendar"
    needs_start = action_key in {"executar", "finalizar"}
    needs_end = action_key in {"finalizar"}
    start = validate_ixc_datetime(request.data_inicio, "data_inicio", required=needs_schedule or needs_start)
    end = validate_ixc_datetime(request.data_final, "data_final", required=needs_end)
    if needs_schedule and not end:
        end = start
    if needs_schedule and start:
        start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S") if end else start_dt
        if end_dt < start_dt:
            raise HTTPException(status_code=400, detail="Data final nao pode ser antes da data inicial.")
        now_dt = datetime.now(monitor.TZ).replace(tzinfo=None)
        if start_dt < now_dt - timedelta(minutes=2):
            raise HTTPException(
                status_code=400,
                detail="Data/hora inicial ja passou. Para agendar no IXC, escolha um horario futuro.",
            )
        if end_dt - start_dt > timedelta(hours=8):
            raise HTTPException(
                status_code=400,
                detail="Janela de agendamento muito longa. Use uma janela de ate 8 horas para gravar no IXC.",
            )
    elif start and end and end < start:
        raise HTTPException(status_code=400, detail="Data final nao pode ser antes da data inicial.")
    message = (request.mensagem or "").strip()
    if action_key in {"mensagem", "finalizar", "reabrir", "marcar_reagendamento"} and not message:
        raise HTTPException(status_code=400, detail="Observacao/mensagem e obrigatoria para esta acao.")

    payload = {
        "id_chamado": os_id,
        "id_resposta": "",
        "mensagem": message or f"Acao {action_key} solicitada pelo Kanban OS Tecnicos VIP.",
        "id_tecnico": request.id_tecnico or "",
        "id_equipe": "",
        "status": action["status"],
        "data": "",
        "id_evento": "",
        "latitude": "",
        "longitude": "",
        "gps_time": "",
    }
    if action_key == "agendar":
        payload.update(
            {
                "data_agendamento": ixc_action_datetime(start),
                "data_agendamento_final": ixc_action_datetime(end),
                "id_compromisso": "",
            }
        )
    if action_key in {"executar", "finalizar", "analisar", "mensagem"}:
        payload.update({"data_inicio": ixc_action_datetime(start), "data_final": ixc_action_datetime(end)})
    if action_key == "encaminhar":
        payload.update({"id_setor": request.id_setor or "", "id_assunto": ""})
    if action_key == "mensagem":
        payload.update(
            {
                "tipo_cobranca": "NENHUM",
                "id_evento_status": "",
                "id_proxima_tarefa": "",
                "finaliza_processo": "N",
            }
        )
    if action_key == "marcar_reagendamento":
        payload.update({"id_setor": request.id_setor or "", "id_compromisso": ""})

    return action_key, action, payload


@app.post("/api/os/{os_id}/acao")
def executar_acao(os_id: str, request: OsActionRequest, session: dict[str, Any] = Depends(require_session)):
    action_key, action, payload = build_action_payload(os_id, request)
    tech_change = technician_change_info(os_id, payload) if action_key == "agendar" else None
    if not request.confirmar_escrita:
        append_audit(
            "action_dry_run",
            actor=session,
            details={"os_id": os_id, "acao": action_key, "id_tecnico": payload.get("id_tecnico") or ""},
        )
        return {
            "ok": True,
            "mode": "dry-run",
            "message": "Payload de acao validado. Confirme no modal para gravar no IXC.",
            "acao": action_key,
            "endpoint_ixc": action["endpoint"],
            "required_base": action["required"],
            "confirmation_required": WRITE_CONFIRMATION,
            "write_allowed": True,
            "technician_change": tech_change,
            "payload": payload,
        }

    if request.confirmacao != WRITE_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"Confirmacao invalida. Use {WRITE_CONFIRMATION}.")

    started_at = time.perf_counter()
    try:
        ixc_result = post_ixc_action(action["endpoint"], payload)
        verification = verify_ixc_action(os_id, action_key, payload, tech_change=tech_change)
    except HTTPException as exc:
        append_action_log(
            {
                "created_at": datetime.now(monitor.TZ).isoformat(timespec="seconds"),
                "ok": False,
                "actor": public_user(session),
                "acao": action_key,
                "endpoint_ixc": action["endpoint"],
                "payload": payload,
                "error": exc.detail,
            }
        )
        append_audit(
            "action_failed",
            actor=session,
            details={"os_id": os_id, "acao": action_key, "error": exc.detail},
        )
        raise

    _CACHE.clear()
    duration_ms = round((time.perf_counter() - started_at) * 1000)
    append_action_log(
        {
            "created_at": datetime.now(monitor.TZ).isoformat(timespec="seconds"),
            "ok": True,
            "actor": public_user(session),
            "acao": action_key,
            "endpoint_ixc": action["endpoint"],
            "payload": payload,
            "ixc_status_code": ixc_result["status_code"],
            "ixc_response": ixc_result["response"],
            "verification": verification,
            "duration_ms": duration_ms,
        }
    )
    append_audit(
        "action_written",
        actor=session,
        details={
            "os_id": os_id,
            "acao": action_key,
            "endpoint_ixc": action["endpoint"],
            "ok": bool(verification["ok"]),
            "id_tecnico": payload.get("id_tecnico") or "",
            "technician_change": tech_change or {},
        },
    )
    if not verification["ok"]:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "mode": "ixc-write-unverified",
                "message": "IXC respondeu 200, mas a releitura nao confirmou a alteracao esperada.",
                "acao": action_key,
                "endpoint_ixc": action["endpoint"],
                "ixc_status_code": ixc_result["status_code"],
                "duration_ms": duration_ms,
                "verification": verification,
                "payload": payload,
            },
        )
    return {
        "ok": True,
        "mode": "ixc-write",
        "message": "Escrita enviada e confirmada por releitura no IXC.",
        "acao": action_key,
        "endpoint_ixc": action["endpoint"],
        "required_base": action["required"],
        "ixc_status_code": ixc_result["status_code"],
        "duration_ms": duration_ms,
        "ixc_response": ixc_result["response"],
        "verification": verification,
        "payload": payload,
    }


@app.post("/api/os/{os_id}/responsavel")
def disabled_responsavel(os_id: str, session: dict[str, Any] = Depends(require_session)):
    raise HTTPException(
        status_code=403,
        detail=f"Troca de responsavel no IXC desativada neste MVP. OS {os_id} nao foi alterada.",
    )
