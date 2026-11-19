from pathlib import Path
import re
import sys
import json
import asyncio
import os
from datetime import datetime, timezone
from io import BytesIO
from nicegui import run
from nicegui import ui
from planner_page import PlannerPanel
from vibeplanner_auth import (
    create_oauth_state,
    authorization_url,
    exchange_code,
    authenticated_user,
    user_workspace,
)
from cisei_scenario_sdk.notebook_app.agent import (
    initialize_agent, chat as sdk_chat, conversation_state, response_text as sdk_response_text,
)
from cisei_scenario_sdk.notebook_app.workflow import confirm as sdk_confirm, generate as sdk_generate
from cisei_scenario_sdk.notebook_app.state import BuilderState

# Workspace inicial, preservado para compatibilidade.
DEFAULT_WORKSPACE = Path('/workspace/cisei_workspace')

SDK_CODE_ROOT = Path(__import__('cisei_scenario_sdk').__file__).resolve().parent
MAX_UPLOAD = 25 * 1024 * 1024
MAX_PREVIEW = 1024 * 1024
TEXT_EXT = {'.csv', '.toml', '.json', '.txt', '.md', '.yaml', '.yml', '.log', '.py'}

ui.add_head_html('''<style>
body {background:#f5f7fb;color:#17243b;font-family:Inter,Arial,sans-serif}
.q-drawer {background:#101e36!important;color:#d8e3f3}
.q-header {background:white!important;color:#17243b!important;border-bottom:1px solid #e5eaf2}
.portal-card {background:white;border:1px solid #e5eaf2;border-radius:16px;box-shadow:0 8px 24px rgba(18,35,64,.04)}
.nav-item {border-radius:10px;padding:11px 13px;cursor:pointer}
.nav-item:hover {background:#233654}
.chat-bubble {background:#f1f5fb;border-radius:14px;padding:14px 17px}
.file-row {border-bottom:1px solid #eef1f6;padding:9px 2px}
</style>''', shared=True)

# This version is intentionally single-user. Do not expose it to untrusted
# users before JupyterHub OAuth and per-user filesystem authorization exist.


# ===== OAuth 2.0 — Login JupyterHub =====

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
import time

from nicegui import app as nicegui_app

nicegui_app.add_middleware(
    SessionMiddleware,
    secret_key=Path(
        '/run/secrets/vibeplanner_session_secret'
    ).read_text(encoding='utf-8').strip(),
    session_cookie='vibeplanner_oauth_state',
    max_age=600,
    same_site='lax',
    https_only=True,
)

@nicegui_app.get('/login')
async def oauth_login(request: Request):
    state = create_oauth_state()
    request.session.clear()
    request.session['oauth_state'] = state
    request.session['oauth_state_created'] = time.time()
    return RedirectResponse(
        authorization_url(state),
        status_code=302,
    )


# ===== OAuth 2.0 — Callback JupyterHub =====

import secrets
from vibeplanner_sessions import create_session, get_session, revoke_session

@nicegui_app.get('/oauth_callback')
async def oauth_callback(
    request: Request,
    code: str = '',
    state: str = '',
):
    expected = request.session.pop('oauth_state', None)
    created = request.session.pop('oauth_state_created', 0)

    if (
        not expected
        or not state
        or not secrets.compare_digest(expected, state)
        or time.time() - created > 600
        or time.time() < created
    ):
        return RedirectResponse('/auth_error?reason=invalid_state', status_code=303)

    if not code:
        return RedirectResponse('/auth_error?reason=missing_code', status_code=303)

    stage = 'exchange_code'
    try:
        token = await exchange_code(code)

        stage = 'authenticated_user'
        username = await authenticated_user(token)

        stage = 'user_workspace'
        workspace = user_workspace(username)

    except Exception as exc:
        status = getattr(
            getattr(exc, 'response', None),
            'status_code',
            None,
        )

        oauth_error = None
        oauth_description = None

        response = getattr(exc, 'response', None)

        if response is not None:
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    oauth_error = payload.get('error')
                    oauth_description = payload.get('error_description')
            except (ValueError, TypeError):
                pass

        print(
            '[VIBEPLANNER OAUTH ERROR] '
            f'stage={stage} '
            f'type={type(exc).__name__} '
            f'http_status={status} '
            f'oauth_error={oauth_error!r} '
            f'oauth_description={oauth_description!r}',
            flush=True,
        )

        return RedirectResponse(
            '/auth_error?reason=authentication_failed',
            status_code=303,
        )

    # A sessão é mantida no servidor; nenhum token OAuth é enviado ao navegador.
    request.session.clear()
    sid = create_session(username)

    response = RedirectResponse('/', status_code=303)
    response.set_cookie(
        key='vibeplanner_sid',
        value=sid,
        max_age=8 * 60 * 60,
        httponly=True,
        secure=True,  # servido via HTTPS (nginx /dev/vibeplanner/)
        samesite='lax',
        path='/',
    )
    return response

@nicegui_app.get('/healthz')
async def healthz():
    return {'status': 'ok'}

@nicegui_app.get('/auth_error')
async def oauth_error(reason: str = 'unknown'):
    from fastapi.responses import HTMLResponse
    from html import escape
    messages = {
        'invalid_state': 'Estado OAuth inválido ou expirado.',
        'missing_code': 'O JupyterHub não retornou um código de autorização.',
        'authentication_failed': 'Não foi possível autenticar ou autorizar este usuário.',
    }
    message = escape(messages.get(reason, 'Erro de autenticação.'))
    return HTMLResponse(f'<h2>VibePlanner AI</h2><p>{message}</p><a href="login">Tentar novamente</a>', status_code=401)

@nicegui_app.get('/logout')
async def oauth_logout(request: Request):
    # Revoga a sessão local do VibePlanner.
    revoke_session(request.cookies.get('vibeplanner_sid'))
    request.session.clear()

    # Encaminha o navegador para o logout do JupyterHub DEV.
    response = RedirectResponse(
        'https://hub.ciseiplan.netiswork.pro.br/dev/hub/logout',
        status_code=303,
        headers={'Cache-Control': 'no-store'},
    )

    # Exclui o cookie de autenticação do VibePlanner.
    response.delete_cookie(
        key='vibeplanner_sid',
        path='/',
        secure=True,
        httponly=True,
        samesite='lax',
    )

    return response



# Proteção de conexões NiceGUI: autenticação também no WebSocket e nos uploads.
# A página e cada operação de filesystem fazem validações adicionais.
from starlette.datastructures import Headers
from http.cookies import SimpleCookie
from starlette.responses import PlainTextResponse

class VibePlannerConnectionGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope['type']
        if kind not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        path = scope.get('path', '')
        # Arquivos estáticos do framework são públicos; não dão acesso a projetos.
        if kind == 'http' and (path in ('/login', '/oauth_callback', '/auth_error', '/logout', '/healthz', '/favicon.ico')
            or path.startswith(('/_nicegui/static/', '/_nicegui/fonts/', '/_nicegui/libraries/', '/static/'))):
            return await self.app(scope, receive, send)
        cookies = SimpleCookie()
        try:
            cookies.load(Headers(scope=scope).get('cookie', ''))
        except Exception:
            pass
        cookie = cookies.get('vibeplanner_sid')
        sid = cookie.value if cookie else None
        if get_session(sid) is None:
            if kind == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
            elif path == '/':
                await RedirectResponse('/login', status_code=303)(scope, receive, send)
            else:
                await PlainTextResponse('Autenticação necessária', status_code=401)(scope, receive, send)
            return
        return await self.app(scope, receive, send)

nicegui_app.add_middleware(VibePlannerConnectionGuard)

@ui.page('/')
def index(request: Request):
    sid = request.cookies.get('vibeplanner_sid')
    username = get_session(sid)

    if not username:
        ui.navigate.to('/login')
        return

    try:
        workspace = user_workspace(username)
    except (PermissionError, OSError):
        ui.navigate.to('/login')
        return

    workspace_state = {
        'username': username,
        'workspace': workspace,
    }

    def require_authenticated_workspace():
        # Confere a sessão original desta página em cada operação.
        current_username = get_session(sid)

        if current_username != username:
            raise PermissionError(
                'Sessão expirada ou encerrada. Faça login novamente.'
            )

        workspace = user_workspace(current_username)

        if workspace_state['username'] != current_username:
            raise PermissionError('Identidade da sessão alterada.')

        if workspace_state['workspace'] != workspace:
            raise PermissionError('Workspace não autorizado.')

        return workspace

    def selected_workspace():
        return require_authenticated_workspace()

    def projects_root():
        workspace = selected_workspace()
        if workspace is None:
            return None
        return workspace / 'projects'

    def require_projects_root():
        root = projects_root()
        if root is None:
            raise RuntimeError('Selecione um workspace autorizado.')
        return root

    def refresh_workspaces():
        username = get_session(sid)

        if username != workspace_state['username']:
            raise PermissionError('Sessão expirada ou alterada.')

        if not username:
            workspace_state['workspace'] = None
            workspace_status.text = 'Autenticação necessária'
        else:
            try:
                workspace_state['workspace'] = user_workspace(username)
                workspace_status.text = f'Usuário autenticado: {username}'
            except (PermissionError, OSError):
                workspace_state['workspace'] = None
                workspace_status.text = 'Workspace não autorizado'

        refresh()
        on_project_change()

    with ui.row().classes('w-full items-center gap-3 p-4'):
        workspace_status = ui.label(
            'Verificando identidade JupyterHub...'
        ).classes('text-sm text-slate-500')

    with ui.header().classes('h-16 px-6 items-center justify-between'):
        with ui.row().classes('items-center gap-3'):
            ui.icon('hub', size='md').classes('text-blue-700')
            ui.label('VibePlanner AI').classes('text-lg font-bold')
            ui.badge('Engineering Workspace', color='blue-1').props('text-color=blue-9')
        with ui.row().classes('items-center gap-3'):
            ui.label('Portal de planejamento de redes').classes('text-sm text-slate-500')
            ui.button('Sair', icon='logout', on_click=lambda: ui.navigate.to('/logout')).props('flat')

    with ui.left_drawer(value=True).classes('p-5'):
        ui.label('WORKSPACE').classes('text-xs tracking-widest text-slate-400 mb-4')
        nav = {}
        for icon, name in [('dashboard','Visão geral'),('folder_open','Projetos'),('upload_file','Arquivos e dados'),('forum','Assistente IA'),('fact_check','Validação'),('settings_input_antenna','Planejador')]:
            nav[name] = (
                ui.button(name, icon=icon)
                .props('flat no-caps align=left')
                .classes('nav-item w-full text-left text-white')
            )
        ui.space()
        ui.separator().classes('opacity-20')
        ui.label('VibePlanner • Portal POC').classes('text-xs text-slate-400 mt-4')

    with ui.column().classes('w-full p-7 gap-6') as overview_section:
        ui.label('Workspace de engenharia').classes('text-3xl font-bold')
        ui.label('Gerencie projetos, consulte dados e prepare cenários de planejamento.').classes('text-slate-500')
        with ui.row().classes('w-full gap-5 flex-wrap'):
            with ui.column().classes('portal-card p-5 flex-1 min-w-64'):
                ui.label('Projetos disponíveis').classes('text-sm text-slate-500')
                count_label = ui.label('—').classes('text-3xl font-bold')
                ui.label('Diretório compartilhado com JupyterHub').classes('text-xs text-slate-400')
            with ui.column().classes('portal-card p-5 flex-1 min-w-64'):
                ui.label('Integração de armazenamento').classes('text-sm text-slate-500')
                status_label = ui.label('Verificando...').classes('text-xl font-semibold')
                ui.label('/workspace/cisei_workspace').classes('text-xs text-slate-400')

        with ui.row().classes('w-full gap-5 items-start flex-wrap'):
            with ui.column().classes('portal-card p-6 flex-1 min-w-80 gap-4') as projects_card:
                ui.label('Projetos').classes('text-xl font-semibold')
                ui.label('Selecione um projeto para visualizar seus arquivos.').classes('text-sm text-slate-500')
                project_select = ui.select({}, label='Projeto existente').classes('w-full').props('outlined')
                files_container = ui.column().classes('w-full gap-2')
                ui.separator()
                ui.label('Novo projeto').classes('text-base font-semibold')
                project_name = ui.input(label='Identificador do projeto', placeholder='ex.: copel_lte450_novo').classes('w-full').props('outlined')

            with ui.column().classes('portal-card p-6 flex-1 min-w-80 gap-4') as assistant_card:
                ui.label('Assistente de cenários').classes('text-xl font-semibold')
                ui.label('Converse com o agente sobre os requisitos do cenário selecionado.').classes('text-sm text-slate-500')
                with ui.column().classes('chat-bubble w-full'):
                    ui.label('VibePlanner AI').classes('text-sm font-semibold text-blue-800')
                    ui.label('Prepare nodes.csv, converse com o agente, valide, confirme e gere scenario.toml.').classes('text-sm')
                message = ui.textarea(label='Mensagem', placeholder='Descreva o cenário que deseja criar...').classes('w-full').props('outlined autogrow')
                with ui.row().classes('items-center gap-3 flex-wrap'):
                    send_button = ui.button('ENVIAR', icon='send').props('unelevated')
                    download_md_button = ui.button('Baixar histórico (.md)', icon='download').props('outline')
                    download_json_button = ui.button('Baixar histórico (.json)', icon='download').props('outline')
                    new_cycle_button = ui.button('Novo ciclo', icon='restart_alt').props('outline')
                chat_status = ui.label('Selecione um projeto para conversar.').classes('text-xs text-slate-500')
                # Resposta em destaque, imediatamente abaixo de ENVIAR.
                with ui.column().classes('chat-bubble w-full gap-1') as agent_reply_box:
                    ui.label('Resposta do agente IA').classes('text-sm font-semibold text-blue-800')
                    agent_reply_text = ui.label('Aguardando mensagem.').classes('text-sm whitespace-pre-wrap break-words')
                ui.label('Histórico completo da conversa').classes('font-semibold')
                chat_history = ui.column().classes('w-full gap-3')
                with ui.row().classes('items-center gap-3 flex-wrap'):
                    validate_button = ui.button('Verificar cenário', icon='fact_check').props('outline')
                    confirm_button = ui.button('Confirmar cenário', icon='check_circle').props('outline')
                    generate_button = ui.button('Gerar scenario.toml', icon='description').props('unelevated')
                scenario_status = ui.label('Cenário ainda não verificado.').classes('text-sm text-slate-500')
                scenario_report = ui.textarea(label='Estado do cenário').props('outlined readonly autogrow').classes('w-full')
                scenario_report.visible = False
                # Configurações técnicas permanecem internas, não na interface.
                # URL pode ser definida via CISEI_PLANNING_BASE_URL no compose.
                # Workspace corresponde ao volume compartilhado visível no container.

                ui.separator()
                ui.label('Fluxo de construção').classes('font-semibold')
                ui.label('1. Dados → 2. Conversa → 3. Validação → 4. Confirmação → 5. scenario.toml').classes('text-sm text-slate-500')

        with ui.column().classes('portal-card p-6 w-full gap-4') as explorer_card:
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Arquivos e dados').classes('text-xl font-semibold')
                ui.badge('Explorador do projeto', color='blue-1').props('text-color=blue-9')
            ui.label('Navegue nas pastas, envie arquivos, visualize textos e baixe documentos.').classes('text-sm text-slate-500')
            breadcrumb = ui.label('Selecione um projeto').classes('text-sm text-slate-500')
            with ui.row().classes('items-center gap-2 flex-wrap'):
                up_button = ui.button('Voltar', icon='arrow_upward').props('outline')
                refresh_button = ui.button('Atualizar', icon='refresh').props('outline')
                upload = ui.upload(label='Enviar arquivos', multiple=True, auto_upload=True).props('flat bordered').classes('max-w-sm')
            explorer_list = ui.column().classes('w-full gap-0')
            ui.separator()
            ui.label('Visualização de arquivo').classes('font-semibold')
            preview_label = ui.label('Selecione um arquivo de texto.').classes('text-sm text-slate-500')
            preview = ui.textarea().props('outlined readonly autogrow').classes('w-full')
            preview.visible = False

            ui.separator()
            ui.label('Preparação de nodes.csv').classes('text-lg font-semibold')
            ui.label('Utiliza os arquivos de data/ do projeto selecionado. O nodes.csv existente não será sobrescrito.').classes('text-sm text-slate-500')
            with ui.row().classes('items-center gap-3 flex-wrap'):
                prepare_button = ui.button('Preparar nodes.csv', icon='fact_check').props('unelevated color=primary')
                nodes_status = ui.label('Aguardando seleção de projeto.').classes('text-sm text-slate-500')
            nodes_report = ui.textarea(label='Diagnóstico da preparação').props('outlined readonly autogrow').classes('w-full')
            nodes_report.visible = False

        # Planejador integrado: somente leitura nesta primeira etapa.
        with ui.column().classes('portal-card p-6 w-full gap-4') as planner_card:
            planner_panel = PlannerPanel(project_folder=lambda: project_folder())

        def open_planner():
            planner_panel.refresh()
            ui.run_javascript('document.getElementById("c' + str(planner_card.id) + '").scrollIntoView({behavior:"smooth",block:"start"})')

        current_parts = []
        agent_instances = {}  # BuilderState por projeto (não agentes legados)
        active_chat_project = None
        CHAT_FILE = 'vibeplanner_conversation.json'

        def history_path(folder):
            return folder / 'reports' / CHAT_FILE

        def read_history(folder):
            path = history_path(folder)
            if not path.is_file() or path.is_symlink():
                return []
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, list):
                raise ValueError('Histórico inválido: esperado uma lista JSON.')
            return data

        def save_history(folder, entries):
            reports = folder / 'reports'
            if reports.is_symlink():
                raise ValueError('Pasta reports não pode ser link simbólico.')
            reports.mkdir(exist_ok=True)
            path = history_path(folder)
            if path.is_symlink():
                raise ValueError('Histórico não pode ser link simbólico.')
            temp = reports / (CHAT_FILE + '.tmp')
            if temp.is_symlink():
                raise ValueError('Arquivo temporário não pode ser link simbólico.')
            temp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(path)

        def response_text(result):
            return sdk_response_text(result)

        def get_builder_state(folder):
            """Cria uma sessão SDK por projeto, após validar o CSV canônico."""
            key = str(folder.resolve())
            state = agent_instances.get(key)
            if state is not None:
                return state
            from cisei_scenario_sdk.nodes_compiler import NodesCompiler
            compiler = NodesCompiler(folder)
            nodes_path = folder / 'nodes.csv'
            if not nodes_path.is_file() or nodes_path.is_symlink():
                raise RuntimeError('Prepare nodes.csv antes de iniciar o agente.')
            validated_rows = compiler.read_csv(nodes_path)
            state = BuilderState(
                workspace=require_projects_root().parent,
                code_root=SDK_CODE_ROOT,
                project_name=folder.name,
                project_root=folder,
                data_root=folder / 'data',
                nodes_path=nodes_path,
                scenario_path=folder / 'scenario.toml',
                compiler=compiler,
                nodes_ready=True,
                validated_rows=validated_rows,
            )
            initialize_agent(state)
            agent_instances[key] = state
            return state

        def render_chat():
            """Preserva toda a conversa na ordem original, junto ao botão ENVIAR."""
            chat_history.clear()
            agent_reply_text.text = 'Aguardando mensagem.'
            folder = project_folder()
            if folder is None:
                chat_status.text = 'Selecione um projeto para conversar.'
                return
            try:
                entries = read_history(folder)
            except Exception as exc:
                chat_status.text = f'Falha ao carregar histórico: {exc}'
                agent_reply_text.text = f'Falha ao carregar histórico: {exc}'
                return
            chat_status.text = f'Projeto: {folder.name} · {len(entries)} mensagens no ciclo atual'
            replies = [entry for entry in entries if entry.get('role') == 'assistant']
            agent_reply_text.text = (str(replies[-1].get('content', '')) if replies else 'Aguardando mensagem.')
            with chat_history:
                for entry in entries:
                    is_user = entry.get('role') == 'user'
                    with ui.column().classes('w-full chat-bubble gap-1'):
                        ui.label('Você' if is_user else 'VibePlanner AI').classes(
                            'text-sm font-semibold text-slate-700' if is_user
                            else 'text-sm font-semibold text-blue-800')
                        ui.label(str(entry.get('content', ''))).classes('text-sm whitespace-pre-wrap break-words')

        def show_scenario_status(folder, status=None):
            if status is None:
                state = get_builder_state(folder)
                status = conversation_state(state)
            scenario_report.visible = True
            scenario_report.value = json.dumps(status, ensure_ascii=False, indent=2, default=str)
            scenario_status.text = ('Válido: {valid} · Completo: {complete} · Confirmado: {confirmed}'
                                    .format(**{k: status.get(k, False) for k in ('valid', 'complete', 'confirmed')}))

        async def scenario_action(action):
            folder = project_folder()
            if folder is None:
                ui.notify('Selecione um projeto.', type='warning')
                return
            for button in (send_button, validate_button, confirm_button, generate_button, new_cycle_button):
                button.disable()
            try:
                def work():
                    state = get_builder_state(folder)
                    if action == 'confirm':
                        result = sdk_confirm(state)
                    elif action == 'generate':
                        base_url = os.environ.get(
                            'CISEI_PLANNING_BASE_URL',
                            'http://cisei-planning-stack-network-service-1:8080',
                        ).strip()
                        workspace = str(require_projects_root().parent)
                        result = sdk_generate(state, overwrite=False,
                                              base_url=base_url, workspace=workspace)
                    else:
                        result = None
                    return result, conversation_state(state)
                result, status = await asyncio.to_thread(work)
                require_authenticated_workspace()
                show_scenario_status(folder, status)
                if action == 'confirm':
                    answer = response_text(result)
                    entries = read_history(folder)
                    entries.append({'role': 'assistant', 'content': answer,
                                    'timestamp': datetime.now(timezone.utc).isoformat()})
                    save_history(folder, entries)
                    render_chat()
                elif action == 'generate':
                    show_files()
                    render_explorer()
                ui.notify({'validate': 'Estado do cenário atualizado.',
                           'confirm': 'Confirmação processada.',
                           'generate': 'scenario.toml gerado e validado.'}[action], type='positive')
            except Exception as exc:
                scenario_status.text = f'{type(exc).__name__}: {exc}'
                ui.notify(f'Operação não concluída: {exc}', type='negative')
            finally:
                for button in (send_button, validate_button, confirm_button, generate_button, new_cycle_button):
                    button.enable()

        def start_new_cycle():
            folder = project_folder()
            if folder is None:
                ui.notify('Selecione um projeto.', type='warning')
                return
            try:
                # O ciclo anterior deixa de estar disponível para download.
                save_history(folder, [])
                agent_instances.pop(str(folder.resolve()), None)
                message.value = ''
                render_chat()
                ui.notify('Novo ciclo iniciado; histórico anterior descartado.', type='positive')
            except Exception as exc:
                ui.notify(f'Não foi possível iniciar novo ciclo: {exc}', type='negative')

        def export_history(fmt):
            folder = project_folder()
            if folder is None:
                ui.notify('Selecione um projeto.', type='warning')
                return
            try:
                entries = read_history(folder)
                if fmt == 'json':
                    content = json.dumps({'project': folder.name, 'messages': entries}, ensure_ascii=False, indent=2)
                else:
                    chunks = [f'# Conversa VibePlanner AI — {folder.name}']
                    for entry in entries:
                        title = 'Você' if entry.get('role') == 'user' else 'VibePlanner AI'
                        chunks.append(f"## {title} — {entry.get('timestamp', '')}\n\n{entry.get('content', '')}")
                    content = '\n\n'.join(chunks) + '\n'
                ui.download.content(content.encode('utf-8'), f'{folder.name}_conversa.{fmt}', media_type='application/json' if fmt == 'json' else 'text/markdown')
            except Exception as exc:
                ui.notify(f'Falha no download: {exc}', type='negative')

        async def send_chat():
            folder = project_folder()
            if folder is None:
                ui.notify('Selecione um projeto válido.', type='warning')
                return
            text = (message.value or '').strip()
            if not text:
                ui.notify('Escreva uma mensagem.', type='warning')
                return
            name = folder.name
            send_button.disable()
            new_cycle_button.disable()
            try:
                entries = read_history(folder)
                entries.append({'role': 'user', 'content': text, 'timestamp': datetime.now(timezone.utc).isoformat()})
                save_history(folder, entries)
                message.value = ''
                render_chat()
                chat_status.text = 'Agente processando a mensagem...'
                agent_reply_text.text = 'Agente processando a mensagem...'
                def ask_agent():
                    state = get_builder_state(folder)
                    result = sdk_chat(state, text)
                    return result, conversation_state(state)
                try:
                    result, status = await asyncio.to_thread(ask_agent)
                    require_authenticated_workspace()
                    answer = response_text(result)
                    if project_select.value == name:
                        show_scenario_status(folder, status)
                except Exception as exc:
                    answer = f'Não foi possível obter a resposta do agente: {type(exc).__name__}: {exc}'
                entries.append({'role': 'assistant', 'content': answer, 'timestamp': datetime.now(timezone.utc).isoformat()})
                save_history(folder, entries)
                if project_select.value == name:
                    render_chat()
                if answer.startswith('Não foi possível'):
                    ui.notify('Falha ao executar agente; consulte a conversa.', type='negative')
            except Exception as exc:
                ui.notify(f'Erro no histórico: {exc}', type='negative')
            finally:
                send_button.enable()
                new_cycle_button.enable()


        def project_folder():
            name = project_select.value
            if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name):
                return None
            root = projects_root()
            if root is None:
                return None
            path = root / name
            if not path.is_dir() or path.is_symlink():
                return None
            return path

        def safe_entry(parts):
            base = project_folder()
            if base is None:
                return None
            path = base.joinpath(*parts)
            # Disallow symlinks at every level; never follow links outside the project.
            if any((base.joinpath(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1)):
                return None
            if not path.resolve().is_relative_to(base.resolve()):
                return None
            return path

        def refresh():
            root = projects_root()
            if root is not None and root.is_dir():
                names = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink() and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', p.name))
                project_select.options = {n: n for n in names}
                count_label.text = str(len(names))
                status_label.text = 'Conectado'
            else:
                project_select.options = {}
                count_label.text = '0'
                status_label.text = 'Indisponível'
            project_select.update()

        def show_files():
            files_container.clear()
            folder = project_folder()
            if folder is None:
                return
            with files_container:
                ui.label('Arquivos e pastas').classes('font-semibold')
                for p in sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                    if p.is_symlink():
                        continue
                    with ui.row().classes('w-full items-center gap-2 py-1'):
                        ui.icon('folder' if p.is_dir() else 'description').classes('text-blue-600')
                        ui.label(p.name).classes('text-sm')

        def show_preview(parts):
            path = safe_entry(parts)
            if path is None or not path.is_file() or path.suffix.lower() not in TEXT_EXT:
                ui.notify('Prévia disponível apenas para arquivos de texto compatíveis.', type='warning')
                return
            if path.stat().st_size > MAX_PREVIEW:
                ui.notify('Arquivo maior que 1 MB: use Download.', type='warning')
                return
            try:
                preview.value = path.read_text(encoding='utf-8')
                preview_label.text = '/'.join(parts)
                preview.visible = True
            except (OSError, UnicodeError) as exc:
                ui.notify(f'Não foi possível visualizar: {exc}', type='negative')

        def download_file(parts):
            path = safe_entry(parts)
            if path is None or not path.is_file():
                ui.notify('Arquivo indisponível.', type='warning')
                return
            ui.download(path, filename=path.name)

        def confirm_delete(parts):
            path_to_delete = safe_entry(parts)
            folder = project_folder()

            if (
                folder is None
                or path_to_delete is None
                or path_to_delete.is_symlink()
                or not path_to_delete.is_file()
            ):
                ui.notify('Arquivo inválido ou indisponível.', type='warning')
                return

            original_project = folder.resolve()
            original_workspace = workspace_state['username']
            filename = path_to_delete.name

            with ui.dialog() as dialog, ui.card():
                ui.label('Excluir arquivo?').classes('text-lg font-bold')
                ui.label(
                    f'O arquivo {filename} será excluído permanentemente.'
                )
                ui.label(
                    'Esta operação não poderá ser desfeita.'
                ).classes('text-red-600')

                def delete_confirmed():
                    dialog.close()
                    try:
                        current_project = project_folder()
                        target = safe_entry(parts)

                        if (
                            current_project is None
                            or current_project.resolve() != original_project
                            or workspace_state['username'] != original_workspace
                            or target is None
                            or target != path_to_delete
                            or target.is_symlink()
                            or not target.is_file()
                        ):
                            raise ValueError(
                                'O projeto ou arquivo mudou. Exclusão cancelada.'
                            )

                        target.unlink()
                        agent_instances.pop(str(original_project), None)
                        show_files()
                        render_explorer()
                        ui.notify(
                            f'Arquivo excluído: {filename}',
                            type='positive',
                        )
                    except (OSError, ValueError) as exc:
                        ui.notify(
                            f'Exclusão não realizada: {exc}',
                            type='negative',
                        )

                with ui.row().classes('w-full justify-end gap-2'):
                    ui.button(
                        'Cancelar',
                        on_click=dialog.close,
                    ).props('flat')
                    ui.button(
                        'Excluir',
                        icon='delete_forever',
                        on_click=delete_confirmed,
                    ).props('color=negative')

            dialog.open()

        def open_folder(parts):
            path = safe_entry(parts)
            if path is None or not path.is_dir():
                ui.notify('Pasta indisponível.', type='warning')
                return
            current_parts[:] = parts
            render_explorer()

        def render_explorer():
            explorer_list.clear()
            preview.visible = False
            folder = safe_entry(current_parts)
            breadcrumb.text = f"{project_select.value or 'Selecione um projeto'} / " + ' / '.join(current_parts)
            if folder is None or not folder.is_dir():
                with explorer_list:
                    ui.label('Selecione um projeto válido para navegar.').classes('text-slate-500')
                return
            with explorer_list:
                try:
                    entries = sorted((p for p in folder.iterdir() if not p.is_symlink()), key=lambda p: (not p.is_dir(), p.name.lower()))
                except OSError as exc:
                    ui.label(f'Falha ao listar: {exc}').classes('text-red-600')
                    return
                if not entries:
                    ui.label('Esta pasta está vazia.').classes('text-slate-400')
                for entry in entries:
                    parts = [*current_parts, entry.name]
                    with ui.row().classes('file-row w-full items-center gap-3'):
                        ui.icon('folder' if entry.is_dir() else 'description').classes('text-blue-600')
                        ui.label(entry.name).classes('flex-1 text-sm')
                        if entry.is_dir():
                            ui.button('Abrir', icon='folder_open', on_click=lambda p=parts: open_folder(p)).props('flat dense')
                        elif entry.is_file():
                            if entry.suffix.lower() in TEXT_EXT:
                                ui.button('Visualizar', icon='visibility', on_click=lambda p=parts: show_preview(p)).props('flat dense')
                            ui.button('Baixar', icon='download', on_click=lambda p=parts: download_file(p)).props('flat dense')
                            ui.button(
                                icon='delete',
                                on_click=lambda p=parts: confirm_delete(p),
                            ).props('flat dense color=negative').tooltip('Excluir arquivo')

        def on_project_change(_=None):
            current_parts.clear()
            show_files()
            render_explorer()
            render_chat()
            planner_panel.refresh()

        def create_project():
            name = (project_name.value or '').strip()
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name):
                ui.notify('Use letras, números, _ ou - (até 64 caracteres).', type='warning')
                return
            root = projects_root()
            if root is None:
                ui.notify('Selecione um workspace.', type='warning')
                return
            path = root / name
            if path.exists() or path.is_symlink():
                ui.notify('O projeto já existe.', type='warning')
                return
            try:
                # Inicialização automática do diretório de projetos
                # no workspace individual do usuário autenticado.
                if root.is_symlink():
                    raise OSError('Diretório projects não pode ser link simbólico.')

                root.mkdir(mode=0o775, parents=True, exist_ok=True)

                # Não sobrescrever projetos existentes.
                path.mkdir(mode=0o775, exist_ok=False)

                for sub in ('data', 'reports', 'results'):
                    (path / sub).mkdir(mode=0o775)
            except OSError as exc:
                ui.notify(f'Falha ao criar projeto: {exc}', type='negative')
                return
            refresh()
            project_select.value = name
            on_project_change()
            project_name.value = ''
            ui.notify('Projeto criado no workspace compartilhado.', type='positive')

        async def upload_file(e):
            folder = safe_entry(current_parts)
            if folder is None or not folder.is_dir():
                ui.notify('Selecione um projeto e uma pasta antes do upload.', type='warning')
                return
            try:
                if hasattr(e, 'file'):
                    filename = e.file.name
                    data = await e.file.read()
                else:
                    filename = e.name
                    data = e.content.read()
                if not filename or Path(filename).name != filename or filename in ('.', '..') or '\\' in filename:
                    raise ValueError('Nome de arquivo inválido')
                if len(data) > MAX_UPLOAD:
                    raise ValueError('Arquivo excede 25 MB')
                require_authenticated_workspace()
                target = safe_entry([*current_parts, filename])
                if target is None or target.exists() or target.is_symlink():
                    raise ValueError('Arquivo já existe ou destino inválido; nada foi sobrescrito')
                with target.open('xb') as f:
                    f.write(data)
                ui.notify(f'Arquivo enviado: {filename}', type='positive')
                render_explorer()
                show_files()
            except (OSError, ValueError) as exc:
                ui.notify(f'Upload não realizado: {exc}', type='negative')

        async def prepare_nodes():
            folder = project_folder()
            if folder is None:
                ui.notify('Selecione um projeto válido.', type='warning')
                return
            # O compilador pode levar tempo; não bloquear a interface NiceGUI.
            prepare_button.disable()
            nodes_status.text = 'Preparando e validando...'
            nodes_report.visible = True
            nodes_report.value = ''
            try:
                def compile_project():
                    from cisei_scenario_sdk.nodes_compiler import NodesCompiler
                    compiler = NodesCompiler(folder)
                    sources = compiler.inspect_sources()
                    if not sources and not (folder / 'nodes.csv').is_file():
                        raise ValueError('Nenhuma fonte encontrada em data/ e nodes.csv não existe.')
                    output = compiler.prepare_output(overwrite_existing=False)
                    report = compiler.last_report.as_dict() if compiler.last_report is not None else {}
                    return output, sources, report

                output, sources, report = await asyncio.to_thread(compile_project)
                require_authenticated_workspace()
                nodes_status.text = 'Preparação concluída'
                nodes_report.value = (f'Projeto: {folder.name}\n'
                                      f'Arquivo: {output.name}\n'
                                      f'Fontes identificadas: {len(sources)}\n\n'
                                      + json.dumps(report, ensure_ascii=False, indent=2, default=str))
                show_files()
                render_explorer()
                agent_instances.pop(str(folder.resolve()), None)
                ui.notify('nodes.csv preparado ou validado com sucesso.', type='positive')
            except Exception as exc:
                nodes_status.text = 'Preparação não concluída'
                nodes_report.value = f'{type(exc).__name__}: {exc}\n\nVerifique as fontes em data/ e tente novamente.'
                ui.notify('Não foi possível preparar nodes.csv. Consulte o diagnóstico.', type='negative')
            finally:
                prepare_button.enable()

        prepare_button.on('click', prepare_nodes)
        send_button.on('click', send_chat)
        validate_button.on('click', lambda: scenario_action('validate'))
        confirm_button.on('click', lambda: scenario_action('confirm'))
        generate_button.on('click', lambda: scenario_action('generate'))
        download_md_button.on('click', lambda: export_history('md'))
        download_json_button.on('click', lambda: export_history('json'))
        new_cycle_button.on('click', start_new_cycle)
        project_select.on_value_change(on_project_change)
        with projects_card:
            ui.button('Criar projeto', icon='add', on_click=create_project).props('unelevated color=primary')
            ui.button('Abrir Planejador', icon='settings_input_antenna', on_click=open_planner).props('outline color=primary')
        up_button.on('click', lambda: open_folder(current_parts[:-1]))
        refresh_button.on('click', lambda: (refresh(), render_explorer(), show_files()))
        upload.on_upload(upload_file)
        nav['Visão geral'].on(
            'click',
            lambda: ui.run_javascript('document.getElementById("c' + str(overview_section.id) + '").scrollIntoView({behavior:"smooth",block:"start"})'),
        )
        nav['Validação'].on(
            'click',
            lambda: ui.run_javascript('document.getElementById("c' + str(validate_button.id) + '").scrollIntoView({behavior:"smooth",block:"start"})'),
        )
        nav['Arquivos e dados'].on('click', lambda: ui.run_javascript('document.getElementById("c' + str(explorer_card.id) + '").scrollIntoView({behavior:"smooth",block:"start"})'))
        nav['Projetos'].on('click', lambda: ui.run_javascript('document.getElementById("c' + str(projects_card.id) + '").scrollIntoView({behavior:"smooth",block:"start"})'))
        nav['Assistente IA'].on('click', lambda: ui.run_javascript('document.getElementById("c' + str(assistant_card.id) + '").scrollIntoView({behavior:"smooth",block:"start"})'))
        nav['Planejador'].on('click', open_planner)
        refresh()
        render_explorer()
        render_chat()
        planner_panel.refresh()
        refresh_workspaces()
ui.run(
    host='0.0.0.0',
    port=8080,
    title='VibePlanner AI',
    reload=False,
    storage_secret=Path(
        '/run/secrets/vibeplanner_session_secret'
    ).read_text(encoding='utf-8').strip(),
)
