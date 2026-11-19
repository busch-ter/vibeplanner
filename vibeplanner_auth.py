"""Autenticação OAuth 2.0 do VibePlanner via JupyterHub DEV."""

import os
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx

from jupyterhub_workspaces import authorized_workspaces


HUB_API_URL = os.environ.get(
    "VIBEPLANNER_JUPYTERHUB_API_URL",
    "http://172.17.0.1:8000/dev/hub/api",
).rstrip("/")

HUB_PUBLIC_URL = os.environ.get(
    "VIBEPLANNER_JUPYTERHUB_PUBLIC_URL",
    "https://hub.ciseiplan.netiswork.pro.br/dev/hub",
).rstrip("/")

OAUTH_CALLBACK_URL = os.environ.get(
    "VIBEPLANNER_OAUTH_CALLBACK_URL",
    "https://hub.ciseiplan.netiswork.pro.br/dev/vibeplanner/oauth_callback",
)

OAUTH_CLIENT_ID = "service-vibeplanner"


def create_oauth_state():
    """Cria identificador aleatório para proteção contra CSRF."""
    return secrets.token_urlsafe(32)


def authorization_url(state):
    """Constrói a URL de autorização do JupyterHub."""
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_CALLBACK_URL,
        "response_type": "code",
        "state": state,
    }
    return f"{HUB_PUBLIC_URL}/api/oauth2/authorize?{urlencode(params)}"


async def exchange_code(code):
    """Troca o código de autorização por um token OAuth."""
    client_secret = Path(
        "/run/secrets/vibeplanner_jupyterhub_token"
    ).read_text(encoding="utf-8").strip()

    if not client_secret:
        raise RuntimeError("Credencial OAuth do serviço vazia.")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{HUB_API_URL}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": OAUTH_CALLBACK_URL,
            },
        )
        response.raise_for_status()
        data = response.json()

    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("JupyterHub não retornou access_token.")

    return token


async def authenticated_user(token):
    """Obtém a identidade autenticada usando o token do usuário."""
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{HUB_API_URL}/user",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()

    username = data.get("name")
    if not isinstance(username, str) or not username:
        raise RuntimeError("Identidade OAuth inválida.")

    return username


def user_workspace(username):
    """Resolve exclusivamente o workspace mapeado para a identidade OAuth."""
    workspaces = authorized_workspaces()
    workspace = workspaces.get(username)

    if workspace is None:
        raise PermissionError("Usuário sem workspace autorizado.")

    workspace = Path(workspace)

    if workspace.is_symlink() or not workspace.is_dir():
        raise PermissionError("Workspace inexistente ou inválido.")

    return workspace.resolve()
