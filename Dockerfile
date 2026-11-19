
FROM zauberzeug/nicegui:latest

USER root

# Wheels locais dos SDKs CISEI
COPY wheels/ /tmp/cisei-wheels/

# Habilita pip no ambiente virtual do NiceGUI
RUN /opt/venv/bin/python -m ensurepip --upgrade

# Instala os SDKs utilizados pelo portal
RUN /opt/venv/bin/python -m pip install \
    --index-url https://pypi.org/simple \
    "openai==3.40.0" \
    /tmp/cisei-wheels/cisei_features_sdk-0.1.1-py3-none-any.whl \
    /tmp/cisei-wheels/cisei_planning_sdk-0.1.36-py3-none-any.whl \
    /tmp/cisei-wheels/cisei_scenario_sdk-0.1.3.dev12-py3-none-any.whl \
    && /opt/venv/bin/python -m pip check \
    && rm -rf /tmp/cisei-wheels

# Verifica os módulos necessários à integração
RUN /opt/venv/bin/python -c \
    "from cisei_scenario_sdk.notebook_app.agent import initialize_agent, chat, conversation_state; \
     from cisei_scenario_sdk.notebook_app.workflow import confirm, generate; \
     from cisei_scenario_sdk.notebook_app.state import BuilderState; \
     print('CISEI Scenario SDK 0.1.3.dev12: OK')"

WORKDIR /app
