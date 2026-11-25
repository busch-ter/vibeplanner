"""Painel NiceGUI de planejamento via cisei_planning_sdk.

Não executa RF automaticamente. Sessões e progresso são locais ao processo NiceGUI.
O portal deve continuar restrito a usuários autorizados (conforme main.py).
"""
from __future__ import annotations

import asyncio
import html
import csv
import inspect
import json
import os
import queue
import threading
import time
import tomllib
from pathlib import Path

from nicegui import ui


class PlannerPanel:
    def __init__(self, project_folder):
        self.project_folder = project_folder
        self._sessions = {}
        self._locks = {}
        self._progress = queue.Queue()
        self._active_key = None
        ui.label('Planejador de Rede').classes('text-xl font-semibold')
        ui.label('Projeto compartilhado com o Criador de Cenários. Cada etapa é iniciada explicitamente.').classes('text-sm text-slate-500')
        self.content = ui.column().classes('w-full gap-3')
        self._timer = ui.timer(0.5, self._drain_progress)

    def _authorized_download(self, path):
        folder = self.project_folder()
        if folder is None or path.parent.resolve() != folder.resolve() or path.is_symlink() or not path.is_file():
            raise PermissionError('Download não autorizado.')
        ui.download(path, filename=path.name)

    def _drain_progress(self):
        while True:
            try:
                key, message = self._progress.get_nowait()
            except queue.Empty:
                break
            if key == self._active_key and hasattr(self, '_status'):
                self._status.text = message

    @staticmethod
    def _open_sdk(folder, planner):
        from cisei_planning_sdk import PlanningClient, PlanningProject
        url = os.environ.get('CISEI_PLANNING_BASE_URL', 'http://cisei-planning-stack-network-service-1:8080').strip()
        client = PlanningClient(url, workspace=str(folder.parent.parent), timeout=7200.0)
        project = PlanningProject(str(folder))
        return client, project, client.open_project(project, planner=planner)

    @staticmethod
    def _plot_html(result, kind, path):
        """SDK 0.1.36 plot_network saves a complete interactive HTML document."""
        if path.is_symlink():
            raise RuntimeError('Destino do mapa é link simbólico.')
        if not isinstance(result, dict):
            raise RuntimeError('O resultado do SDK não é um dicionário.')
        from cisei_planning_sdk import plot_network
        plot_network(result, kind=kind, display=False, save_path=path)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError('O SDK não gerou o mapa HTML.')
        document = path.read_text(encoding='utf-8')
        if '<html' not in document[:1024].lower():
            raise RuntimeError('O arquivo gerado não contém um documento HTML.')
        return ('map', kind, document)

    @staticmethod
    def _summary(data):
        if isinstance(data, dict):
            parts = [f'{k}: {len(v)}' for k, v in data.items() if isinstance(v, (list, tuple, dict))]
            return ' | '.join(parts[:12]) or ', '.join(map(str, list(data)[:12]))
        return str(data)[:1000]

    @staticmethod
    def _validate(folder):
        errors = []
        scenario_path, nodes_path = folder / 'scenario.toml', folder / 'nodes.csv'
        if any(p.is_symlink() for p in (scenario_path, nodes_path)):
            return 0, ['Arquivo simbólico detectado.']
        if not scenario_path.is_file() or not nodes_path.is_file():
            return 0, ['Arquivos ausentes: ' + ', '.join(p.name for p in (scenario_path, nodes_path) if not p.is_file())]
        scenario = None
        try:
            with scenario_path.open('rb') as f:
                scenario = tomllib.load(f)
            inst = scenario.get('instances', {})
            if inst.get('source') != 'nodes.csv':
                errors.append('instances.source deve ser nodes.csv.')
            if inst.get('format') != 'csv':
                errors.append('instances.format deve ser csv.')
            if not isinstance(inst.get('profile_column'), str) or not inst['profile_column']:
                errors.append('instances.profile_column ausente.')
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            errors.append(f'scenario.toml: {exc}')
        count = 0
        try:
            with nodes_path.open(encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    errors.append('nodes.csv sem cabeçalho.')
                elif scenario:
                    profile = scenario.get('instances', {}).get('profile_column')
                    if profile and profile not in reader.fieldnames:
                        errors.append(f'Coluna {profile!r} ausente em nodes.csv.')
                count = sum(1 for _ in reader)
            if not count:
                errors.append('nodes.csv sem registros.')
        except (OSError, UnicodeError, csv.Error) as exc:
            errors.append(f'nodes.csv: {exc}')
        return count, errors

    def refresh(self):
        self.content.clear()
        folder = self.project_folder()
        self._active_key = str(folder.resolve()) if folder else None
        with self.content:
            if folder is None:
                ui.label('Selecione um projeto no painel Projetos.').classes('text-amber-700')
                return
            key = self._active_key
            ui.label(f'Projeto ativo: {folder.name}').classes('text-lg font-semibold')
            count, errors = self._validate(folder)
            with ui.row().classes('gap-4 flex-wrap'):
                with ui.card().classes('min-w-48'):
                    ui.label('Nós no CSV').classes('text-sm text-slate-500')
                    ui.label(str(count)).classes('text-2xl font-bold')
                with ui.card().classes('min-w-48'):
                    ui.label('Verificação básica').classes('text-sm text-slate-500')
                    ui.label('Pendências' if errors else 'Sem pendências básicas').classes('text-lg font-semibold')
            for err in errors:
                ui.label('• ' + err).classes('text-red-700')
            if not errors:
                ui.label('Inspeção básica concluída; não substitui preflight/validação completa do SDK.').classes('text-sm text-amber-700')

            self._status = ui.label('Sessão SDK aberta.' if key in self._sessions else 'Projeto ainda não aberto no SDK.').classes('text-sm whitespace-pre-wrap')
            details = ui.label('').classes('text-sm whitespace-pre-wrap')
            planner_type = ui.select(
                {'cell': 'Cell — LTE / LoRaWAN',
                 'graph': 'Graph — Mesh / Multissalto'},
                value='cell',
                label='Tipo de planejador'
            ).props('outlined').classes('w-72')

            limit = ui.number('Distância máxima (m)', value=5000, min=1, step=100).classes('w-52').props('outlined')
            preserve = ui.checkbox('Preservar candidatos existentes', value=False)
            batch = ui.number('Tamanho do lote', value=20, min=1, step=1).classes('w-44').props('outlined')
            geo_timeout = ui.number('Timeout geográfico (s)', value=7200, min=1, step=100).classes('w-52').props('outlined')
            pool = ui.number('Pool geográfico', value=1, min=1, step=1).classes('w-44').props('outlined')
            threshold = ui.number('Limiar de avaliação (rank)', value=2, min=0, step=0.5).classes('w-52').props('outlined')

            ui.separator()
            ui.label('01 · Preparação e parâmetros').classes('text-lg font-semibold text-blue-900')
            ui.label('Abra o projeto e ajuste os parâmetros antes de executar as etapas.').classes('text-sm text-slate-500')
            with ui.row().classes('w-full gap-3 items-center flex-wrap'):
                open_button = ui.button('Abrir projeto no SDK', icon='folder_open').props('unelevated color=primary')
                refresh_button = ui.button('Atualizar arquivos', icon='refresh').props('outline')
            ui.separator()
            ui.label('02 · Execução do planejamento').classes('text-lg font-semibold text-blue-900')
            ui.label('Execute cada etapa explicitamente, na ordem indicada.').classes('text-sm text-slate-500')
            with ui.grid(columns=2).classes('w-full gap-3'):
                with ui.card().classes('w-full gap-2'):
                    ui.label('1. Candidatos').classes('font-semibold')
                    ui.label('Construir a topologia de enlaces possíveis.').classes('text-xs text-slate-500')
                    candidate_button = ui.button('Construir candidatos', icon='account_tree').props('outline').classes('w-full')
                with ui.card().classes('w-full gap-2'):
                    ui.label('2. Métricas RF').classes('font-semibold')
                    ui.label('Calcular as métricas geográficas e de rádio.').classes('text-xs text-slate-500')
                    metrics_button = ui.button('Calcular métricas RF', icon='analytics').props('outline').classes('w-full')
                with ui.card().classes('w-full gap-2'):
                    ui.label('3. Solução').classes('font-semibold')
                    ui.label('Determinar os enlaces da rede planejada.').classes('text-xs text-slate-500')
                    solve_button = ui.button('Solucionar rede', icon='device_hub').props('outline').classes('w-full')
                with ui.card().classes('w-full gap-2'):
                    ui.label('4. Avaliação').classes('font-semibold')
                    ui.label('Avaliar e persistir os resultados.').classes('text-xs text-slate-500')
                    evaluate_button = ui.button('Avaliar e salvar', icon='fact_check').props('outline').classes('w-full')
            ui.separator()
            ui.label('03 · Mapas e análise').classes('text-lg font-semibold text-blue-900')
            with ui.row().classes('w-full gap-3 flex-wrap'):
                candidate_map_button = ui.button('Mapa de candidatos', icon='map').props('outline')
                map_button = ui.button('Mapa final', icon='map').props('outline')
                saved_candidate_button = ui.button('Candidatos salvos', icon='account_tree').props('outline')
                saved_map_button = ui.button('Resultado salvo', icon='visibility').props('outline')
            with ui.row().classes('w-full gap-3 flex-wrap'):
                links_button = ui.button('Inspecionar enlaces', icon='list').props('unelevated color=primary')
                reload_button = ui.button('Carregar resultado salvo', icon='description').props('outline')
            all_buttons = [open_button, candidate_button, metrics_button, solve_button,
                           evaluate_button, map_button, candidate_map_button, links_button, reload_button, saved_candidate_button, saved_map_button]
            lock = self._locks.setdefault(key, threading.Lock())

            def sync_buttons():
                busy = lock.locked()
                planner_type.disable() if busy or key in self._sessions else planner_type.enable()
                has_session = key in self._sessions
                for b in all_buttons:
                    allowed = not busy and not errors and (b in (open_button, saved_candidate_button, saved_map_button) or has_session)
                    b.enable() if allowed else b.disable()

            ui.separator()
            ui.label('Navegador de enlaces').classes('text-lg font-semibold')
            ui.label('Selecione um enlace para consultar os campos disponibilizados pelo SDK.').classes('text-sm text-slate-500')
            with ui.row().classes('items-center gap-3 flex-wrap'):
                link_source = ui.select({'candidate': 'Candidatos', 'solution': 'Planejados'}, value='candidate', label='Conjunto').props('outlined').classes('w-44')
                link_index = ui.number('Índice do enlace (inicia em 1)', value=1, min=1, step=1).props('outlined').classes('w-56')
                link_prev = ui.button('Anterior', icon='chevron_left').props('outline')
                link_next = ui.button('Próximo', icon='chevron_right').props('outline')
            link_count = ui.label('Clique em Inspecionar enlaces para carregar a sessão.').classes('text-sm text-slate-500')
            link_info = ui.column().classes('w-full gap-2')
            link_sets = {'candidate': [], 'solution': []}

            def normalize_links(value):
                if isinstance(value, (list, tuple)):
                    return list(value)
                if isinstance(value, dict):
                    for k in ('links', 'edges', 'items', 'data', 'candidate_links', 'solution_links'):
                        if isinstance(value.get(k), (list, tuple)):
                            return list(value[k])
                    return [value]
                return list(value) if value is not None and hasattr(value, '__iter__') and not isinstance(value, (str, bytes)) else []

            def render_link():
                items = link_sets[link_source.value]
                link_info.clear()
                if not items:
                    link_count.text = 'Nenhum enlace disponível neste conjunto.'
                    return
                index = max(0, min(len(items) - 1, int(link_index.value or 1) - 1))
                if link_index.value != index + 1:
                    link_index.value = index + 1
                link_count.text = f'Enlace {index + 1} de {len(items)} — {"candidatos" if link_source.value == "candidate" else "planejados"}'
                item = items[index]
                with link_info:
                    if isinstance(item, dict):
                        for field, value in item.items():
                            with ui.row().classes('w-full items-start gap-3 border-b border-slate-100 py-1'):
                                ui.label(str(field)).classes('font-semibold text-sm min-w-40')
                                text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list, tuple)) else str(value)
                                ui.label(text[:2000]).classes('text-sm break-all flex-1')
                    else:
                        ui.label(str(item)[:4000]).classes('text-sm whitespace-pre-wrap')

            def step_link(delta):
                link_index.value = int(link_index.value or 1) + delta
                render_link()

            link_source.on_value_change(lambda _: render_link())
            link_index.on_value_change(lambda _: render_link())
            link_prev.on('click', lambda: step_link(-1))
            link_next.on('click', lambda: step_link(1))

            async def perform(action):
                current = self.project_folder()
                if current is None or str(current.resolve()) != key:
                    ui.notify('O projeto selecionado mudou. Atualize o painel.', type='warning')
                    return
                if errors:
                    ui.notify('Corrija as pendências básicas antes de continuar.', type='warning')
                    return
                if not lock.acquire(blocking=False):
                    ui.notify('Já existe uma operação em andamento para este projeto.', type='warning')
                    return
                sync_buttons()
                started = time.monotonic()
                self._status.text = f'Executando: {action}...'
                details.text = ''
                try:
                    # Copiar parâmetros no thread da UI, antes de iniciar a tarefa.
                    selected_planner = planner_type.value
                    params = dict(limit_m=int(limit.value), preserve_existing=bool(preserve.value),
                                  batch_size=int(batch.value), geo_timeout=float(geo_timeout.value),
                                  geo_pool_size=int(pool.value), rank_threshold=float(threshold.value))
                    if params['limit_m'] <= 0 or params['batch_size'] <= 0 or params['geo_timeout'] <= 0 or params['geo_pool_size'] <= 0 or params['rank_threshold'] < 0:
                        raise ValueError('Parâmetros fora do intervalo permitido.')

                    def work():
                        authorized = self.project_folder()
                        if authorized is None or authorized.resolve() != folder.resolve():
                            raise PermissionError('Sessão ou projeto não autorizado.')
                        if action in ('mapa_salvo', 'mapa_candidatos_salvos'):
                            result_path = folder / 'result.json'
                            if not result_path.is_file() or result_path.is_symlink():
                                raise FileNotFoundError('result.json não encontrado neste projeto.')
                            result = json.loads(result_path.read_text(encoding='utf-8'))
                            kind = 'candidate' if action == 'mapa_candidatos_salvos' else 'final'
                            filename = 'candidate_network.html' if kind == 'candidate' else 'saved_network.html'
                            _, _, document = self._plot_html(result, kind, folder / filename)
                            return ('map', kind if kind == 'candidate' else 'saved', document)
                        if action == 'abrir':
                            self._sessions[key] = self._open_sdk(folder, selected_planner)
                            return f'Sessão aberta com planner={selected_planner}.'
                        if key not in self._sessions:
                            raise RuntimeError('Abra primeiro o projeto no SDK.')
                        client, project, scenario = self._sessions[key]
                        if action == 'candidatos':
                            return self._summary(scenario.build_candidate_edges(
                                limit_m=params['limit_m'], preserve_existing=params['preserve_existing']))
                        if action == 'metricas':
                            def on_batch(progress):
                                self._progress.put((key, f'Cálculo RF em andamento: {progress}'))
                            return self._summary(scenario.compute_metrics(
                                geo_timeout=params['geo_timeout'], geo_pool_size=params['geo_pool_size'],
                                batch_size=params['batch_size'], on_batch=on_batch))
                        if action == 'solucionar':
                            return self._summary(scenario.solve())
                        if action == 'avaliar':
                            # show_evaluation é visualização de notebook; preferir API de dados.
                            evaluation = None
                            if hasattr(scenario, 'evaluate'):
                                evaluation = scenario.evaluate(rank_threshold=params['rank_threshold'])
                            saved = scenario.save_result(rank_threshold=params['rank_threshold'])
                            return 'Avaliação: ' + self._summary(evaluation) + '\nResultado salvo: ' + self._summary(saved)
                        if action in ('mapa_final', 'mapa_candidatos'):
                            kind = 'final' if action == 'mapa_final' else 'candidate'
                            result = scenario.load_result() if kind == 'final' else scenario.export_result()
                            filename = 'solution_network.html' if kind == 'final' else 'candidate_network.html'
                            return self._plot_html(result, kind, folder / filename)
                        if action == 'enlaces':
                            candidates = scenario.get_candidate_links()
                            try:
                                chosen = scenario.get_solution_links()
                            except RuntimeError as exc:
                                if 'No saved solution' not in str(exc):
                                    raise
                                chosen = []
                            return ('links', normalize_links(candidates), normalize_links(chosen))
                        if action == 'carregar':
                            return self._summary(scenario.load_result())
                        raise RuntimeError('Operação desconhecida')

                    result = await asyncio.to_thread(work)
                    authorized = self.project_folder()
                    if authorized is None or authorized.resolve() != folder.resolve():
                        raise PermissionError('Sessão encerrada ou projeto alterado.')
                    if self._active_key == key and isinstance(result, tuple) and result[0] == 'links':
                        link_sets['candidate'] = result[1]
                        link_sets['solution'] = result[2]
                        link_index.value = 1
                        render_link()
                        result = f'{len(result[1])} candidatos e {len(result[2])} enlaces planejados disponíveis para inspeção.'
                    if self._active_key == key and isinstance(result, tuple) and result[0] == 'map':
                        show_map(result[1], result[2])
                        result = 'Mapa interativo exibido no painel.'
                    if self._active_key == key:
                        self._status.text = f'{action.capitalize()} concluído em {time.monotonic()-started:.1f}s.'
                        details.text = result
                    ui.notify(f'Etapa {action} concluída.', type='positive')
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    if self._active_key == key:
                        self._status.text = f'Falha em {action}: {type(exc).__name__}: {exc}'
                    ui.notify(f'Falha na etapa {action}; consulte o painel.', type='negative')
                finally:
                    lock.release()
                    if self._active_key == key:
                        sync_buttons()
                        render_files()

            for b, action in [(open_button, 'abrir'), (candidate_button, 'candidatos'),
                              (metrics_button, 'metricas'), (solve_button, 'solucionar'),
                              (evaluate_button, 'avaliar'), (map_button, 'mapa_final'),
                              (candidate_map_button, 'mapa_candidatos'), (links_button, 'enlaces'),
                              (reload_button, 'carregar'), (saved_candidate_button, 'mapa_candidatos_salvos'),
                              (saved_map_button, 'mapa_salvo')]:
                b.on('click', lambda _, a=action: perform(a))
            refresh_button.on('click', self.refresh)
            ui.label('Ordem recomendada: abrir → candidatos → métricas → solucionar → avaliar/salvar → mapa final. A geração dos mapas e a inspeção são opcionais.').classes('text-sm text-slate-500')
            ui.label('A opção de não preservar candidatos substitui o conjunto anterior. Não inicie a mesma sessão em duas abas simultaneamente.').classes('text-sm text-amber-700')
            ui.separator()
            ui.label('Visualização gráfica da rede').classes('text-lg font-semibold')
            ui.label('Use Exibir candidatas salvas, Gerar mapa de candidatos, Gerar mapa final ou Exibir resultado salvo. O mapa interativo aparece abaixo.').classes('text-sm text-slate-500')
            map_title = ui.label('Nenhum mapa selecionado.').classes('font-medium')
            map_box = ui.column().classes('w-full')

            def show_map(kind, document):
                labels = {'candidate': 'Enlaces candidatos', 'final': 'Rede planejada', 'saved': 'Resultado salvo'}
                map_title.text = labels.get(kind, kind)
                map_box.clear()
                with map_box:
                    iframe = (
                        '<iframe title="Mapa interativo da rede" sandbox="allow-scripts" '
                        'style="display:block;width:100%;height:760px;border:1px solid #d8e2ec;border-radius:10px" '
                        'srcdoc="' + html.escape(document, quote=True) + '"></iframe>'
                    )
                    ui.html(iframe, sanitize=False).classes('w-full')

            ui.separator()
            ui.label('Arquivos do projeto').classes('font-semibold')
            files_box = ui.row().classes('gap-2 flex-wrap')

            def render_files():
                files_box.clear()
                with files_box:
                    for filename in ('result.json', 'candidate_network.html', 'solution_network.html', 'saved_network.html'):
                        path = folder / filename
                        if path.is_file() and not path.is_symlink():
                            ui.button('Baixar ' + filename, icon='download',
                                      on_click=lambda p=path: self._authorized_download(p)).props('outline')
                    result_path = folder / 'result.json'
                    if result_path.is_file() and not result_path.is_symlink():
                        try:
                            if result_path.stat().st_size <= 30 * 1024 * 1024:
                                data = json.loads(result_path.read_text(encoding='utf-8'))
                                if isinstance(data, dict):
                                    ui.label('Resultado salvo: ' + self._summary({k: v for k, v in data.items() if k in ('candidate_edges', 'metric_edges', 'planned_nodes', 'planned_edges', 'redundancy_edges')})).classes('text-sm')
                        except (OSError, UnicodeError, ValueError) as exc:
                            ui.label(f'Falha na prévia: {exc}').classes('text-sm text-amber-700')

            render_files()
            sync_buttons()
