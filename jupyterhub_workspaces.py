"""Integração de leitura com usuários e workspaces do JupyterHub."""

import json
import os
import urllib.request
from pathlib import Path


JUPYTERHUB_API_URL = os.environ.get(
    "VIBEPLANNER_JUPYTERHUB_API_URL",
    "http://JupyterHub:8000/hub/api",
).rstrip("/")

TOKEN_FILE = Path(
    os.environ.get(
        "VIBEPLANNER_JUPYTERHUB_TOKEN_FILE",
        "/run/secrets/vibeplanner_jupyterhub_token",
    )
)

WORKSPACES_FILE = Path(
    os.environ.get(
        "VIBEPLANNER_WORKSPACES_FILE",
        "/app/workspaces.json",
    )
)


def list_active_users():
    """Lista usuários com pelo menos um servidor Jupyter pronto."""

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    if not token:
        raise RuntimeError("Token do JupyterHub vazio.")

    request = urllib.request.Request(
        f"{JUPYTERHUB_API_URL}/users",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        users = json.load(response)

    if not isinstance(users, list):
        raise RuntimeError("Resposta inesperada da API do JupyterHub.")

    return sorted(
        user["name"]
        for user in users
        if isinstance(user, dict)
        and isinstance(user.get("name"), str)
        and user.get("name", "").strip()
    )


def authorized_workspaces():
    """Carrega somente os workspaces explicitamente autorizados."""

    data = json.loads(WORKSPACES_FILE.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise RuntimeError("Mapeamento de workspaces inválido.")

    return {
        username: Path(path)
        for username, path in data.items()
        if isinstance(username, str)
        and isinstance(path, str)
    }


def available_workspaces():
    """Retorna usuários ativos que possuem workspace autorizado."""

    active = set(list_active_users())
    authorized = authorized_workspaces()

    result = {}

    for username, workspace in authorized.items():
        if username not in active:
            continue

        if workspace.is_symlink() or not workspace.is_dir():
            continue

        result[username] = workspace

    return result


def workspace_statuses():
    """Consulta o estado dos single-user servers no JupyterHub."""

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    request = urllib.request.Request(
        f"{JUPYTERHUB_API_URL}/users",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        users = json.load(response)

    statuses = {}

    for user in users:
        username = user.get("name")

        if not username:
            continue

        servers = user.get("servers") or {}

        if not servers:
            status = "Desligado"
        elif any(
            server.get("ready") is True
            for server in servers.values()
            if isinstance(server, dict)
        ):
            status = "Ativo"
        else:
            status = "Iniciando/parando"

        statuses[username] = status

    return statuses
