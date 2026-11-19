# CLAUDE.md — Gestor de Contas

Instruções para o Claude Code neste repositório. O design está em [DESIGN.md](DESIGN.md) e o histórico das versões no [README.md](README.md).

## Protocolo de Arranque e Execução

1. **Inspeção Cirúrgica:** Antes de editar, começa pelo router (`app/routes/<área>.py`), pelo módulo de negócio (`app/<área>.py`), pelo template correspondente e pelo ficheiro de testes (`tests/test_<área>.py`). Abre `app/models.py`, `app/deps.py` ou outros módulos só quando a alteração lhes tocar. Não leias o repositório inteiro.
2. **Alinhamento de Plano:** Para tarefas que envolvam migrações Alembic, alterações de schema ou múltiplos módulos, apresenta um plano curto antes de aplicar edições. Uma migração que já está no git nunca se altera: cria-se outra.
3. **Verificação Incremental:**
   - Durante a iteração, corre apenas os testes do módulo alterado (`python -m pytest -q -p no:warnings tests/test_<módulo>.py`). O hook `testes_do_modulo.py` já os corre depois de cada edição a `app/<módulo>.py`; se falharem, corrige logo.
   - Corre a suite completa (`python -m pytest -q -p no:warnings -n auto`) apenas no fecho de alterações transversais ou antes de preparar uma versão.
4. **Controlo de Estado e Git:**
   - **Não fazer `git commit`, `git push` nem `./release.sh` sem pedido explícito.**
   - Pede confirmação explícita antes de qualquer comando destrutivo ou irreversível (`rm -rf`, `git reset --hard`, `git clean -fd`, alterações manuais em BD).

## Notas Específicas do Repositório

### O que é
- Gestor financeiro doméstico, multiutilizador e self-hosted, para a NAS QNAP. Importa extratos de bancos portugueses (Santander, ActivoBank, Crédito Agrícola, Revolut em PDF, cartões) e gere contas, categorias e regras, relatórios, património, empréstimos, previsões, subscrições, viagens (com o Immich), documentos (com o Paperless-ngx), saúde, Gestão do Lar (Santa Casa e farmácia) e Garagem/Espaços (mapas 2D/3D e inventário).
- Entidades separadas (ex.: Família, Mãe) com papéis Ver / Editar / Administrar. Os módulos opcionais ligam-se por entidade (`MODULOS` em `app/models.py`).

### Stack e estrutura
- Python + FastAPI + Jinja2, renderizado no servidor; SQLAlchemy 2 + SQLite (`gestor.db`); Alembic. Não há build de frontend: o JS e o CSS estão em `app/static/` (a Garagem é uma app JS com three.js em `app/static/garagem/`, servida em `/garagem/app`).
- `app/main.py`: `create_app()` (factory), a lista de routers e **`VERSAO`**.
- `app/routes/*.py`: um router por área. `app/<área>.py`: a lógica de negócio. `app/parsers/`: um parser por banco (um banco novo = `app/parsers/<banco>.py` com `Parser`, acrescentado a `todos()`).
- `app/deps.py`: `render`, `exigir_papel`, `entidade_escolhida`, `flash`, `auditar`. Os formulários lêem-se com `seguranca.ler_form` e levam sempre o campo `csrf`.
- Integrações opcionais, todas desligadas quando falta a variável de ambiente: IA (`app/ia.py`, `ANTHROPIC_API_KEY`, `GESTOR_IA_MODELO`), Paperless (`GESTOR_PAPERLESS_*`), Immich (`GESTOR_IMMICH_*`, mais uma chave pessoal por utilizador, cifrada, guardada em Perfil) e pasta de extratos (`GESTOR_IMPORT_DIR`). Nos testes a API externa é sempre simulada (monkeypatch de `_pedido` / `ia._chamar`).

### Comandos
```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:warnings -n auto          # suite completa em paralelo (pytest-xdist)
python -m pytest -q -p no:warnings tests/test_<módulo>.py
python -m uvicorn --factory app.main:create_app --port 8200 --reload
python -m alembic revision --autogenerate -m "descricao"
```
- Neste Windows, o Python está em `C:\Users\sergi\AppData\Local\Python\bin\python.exe` (3.14; o Docker usa a 3.12). A pré-visualização está em `.claude/launch.json` («gestor-contas», porta 8201, dados em `%TEMP%`).
- Sem `-n auto`, a suite demora mais de 15 minutos: nesse caso, corre-a em background.
- `tests/conftest.py` corre as migrações uma vez para uma BD modelo e torna o argon2 barato. Usa como base as fixtures de `tests/test_app.py` (`PW`, `_csrf`, `_post`, `/setup`).

### Hooks do projeto (`.claude/settings.json`)
- `proteger.py` (antes de Edit/Write) bloqueia `data/`, `*.db*`, `secret.key`, `*.env`/`.env*` e as migrações em `migrations/versions/` que já estejam no git.
- `testes_do_modulo.py` (depois de Edit/Write) corre `tests/test_<módulo>.py` quando editas `app/<módulo>.py` ou um ficheiro de testes.

### Convenções
- Código, nomes, comentários, textos da interface e mensagens de commit em **português de Portugal**. Na interface, as datas são dd/mm/aaaa e os valores em euros com vírgula (filtros `data` e `eur`). Os montantes guardam-se em cêntimos (inteiros).
- Cada alteração visível sobe a versão em `app/main.py` (`VERSAO`) e acrescenta uma entrada «**vX.Y.Z — …**» no topo da lista do README. Mensagens de commit: `vX.Y.Z: Área — resumo`.
- Release (só a pedido): `./release.sh X.Y.Z` (exige a árvore limpa; cria a tag e faz push) → o GitHub Actions publica `ghcr.io/seriocha/gestor-contas:X.Y.Z` e `:latest` → na NAS corre-se `qnap/update.sh` (docker puro, dados em `/share/Container/gestor-contas`). As migrações correm sozinhas no arranque.
- Para patches com várias linhas, escreve o script num ficheiro (com a ferramenta Write). Os heredocs no bash do Windows estragam as barras invertidas.
- Privacidade da IA: só vão descrições de comerciantes, moedas, zonas postais e lugares. Nunca valores, datas, contas, nomes de pessoas, nomes de ficheiros nem coordenadas.
- Uma secção lenta (Immich, Paperless) carrega-se por `fetch` como fragmento (`_*.html`), para não atrasar a página.
- As mensagens da interface aparecem na barra fixa de baixo (`#moldura-mensagens`, `static/mensagens.js`); usa `window.mensagem(texto, tipo)` em vez de `alert()`.
