import asyncio
import contextlib
from datetime import date
from pathlib import Path
from urllib.parse import quote

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import backup, extratos, formatos, motor_docs, pasta_importacao, preferencias, tarefas, tv
from . import patrimonio as pt
from .config import BASE_DIR, carregar_definicoes
from .db import criar_engine, url_sqlite
from .deps import NaoAutenticado, render
from .routes import (abastecer, admin, auth, documentos, inicio, posicoes, categorias, contas, emprestimos, importar, movimentos, painel, pareaveis, previsoes,
                     patrimonio, regras, relatorios, saude, subscricoes, viagens)
from .routes import admin_paperless, tv as rotas_tv
from .seed import semear

APP_DIR = Path(__file__).resolve().parent
VERSAO = "0.81.1"


def aplicar_migracoes(db_path: Path, backup_dir: Path | None = None) -> None:
    """Leva a base à última revisão. Se já existe e vai ser migrada, faz antes uma cópia verificada (backup_dir)."""
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url_sqlite(db_path))
    if backup_dir is not None and db_path.exists() and db_path.stat().st_size:
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from sqlalchemy import create_engine
        eng = create_engine(url_sqlite(db_path))
        try:
            with eng.connect() as c:
                atual = MigrationContext.configure(c).get_current_revision()
        finally:
            eng.dispose()
        destino = ScriptDirectory.from_config(cfg).get_current_head()
        if atual and atual != destino:
            backup.backup_antes_de_migrar(db_path, backup_dir, atual, destino)
    command.upgrade(cfg, "head")


def create_app(data_dir: Path | None = None, backups_automaticos: bool = True) -> FastAPI:
    defs = carregar_definicoes(data_dir)
    aplicar_migracoes(defs.db_path, defs.backup_dir)
    engine = criar_engine(defs.db_path)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as db:
        semear(db)
        prefs = preferencias.carregar(db)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tarefas_fundo = [asyncio.create_task(backup.ciclo_backups(defs, VERSAO)),
                         asyncio.create_task(pt.ciclo_revisoes(SessionLocal, lambda: app.state.prefs)),
                         asyncio.create_task(pasta_importacao.ciclo(SessionLocal, defs, lambda: app.state.prefs)),
                         asyncio.create_task(extratos.ciclo_paperless(SessionLocal, defs, lambda: app.state.prefs)),
                         asyncio.create_task(motor_docs.ciclo(SessionLocal, defs, lambda: app.state.prefs))] \
            if backups_automaticos else []
        yield
        for t in tarefas_fundo:
            t.cancel()

    app = FastAPI(title="Gestor de Contas", version=VERSAO, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.defs = defs
    app.state.SessionLocal = SessionLocal
    app.state.bloqueios = auth.Bloqueios()
    app.state.bloqueios_tv = auth.Bloqueios()      # códigos de emparelhamento errados, por utilizador
    app.state.emparelhamentos = tv.Emparelhamentos()
    app.state.prefs = prefs

    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    templates.env.filters["eur"] = formatos.fmt_montante
    templates.env.filters["data"] = formatos.fmt_data
    templates.env.globals["versao"] = VERSAO
    templates.env.globals["tarefas_janela"] = lambda: tarefas.na_janela(app.state.prefs, date.today())
    templates.env.globals["mostrar_parte"] = lambda: preferencias.ligada(app.state.prefs, "mostrar_minha_parte")
    templates.env.filters["iban"] = lambda v: v if preferencias.ligada(app.state.prefs, "mostrar_iban_completo")         else formatos.mascarar_iban(v)
    templates.env.filters["pct"] = lambda v: "—" if v is None else f"{v:+.1f}%".replace(".", ",")
    app.state.templates = templates

    app.add_middleware(
        SessionMiddleware,
        secret_key=defs.secret_key,
        session_cookie="gestor_sessao",
        max_age=60 * 60 * 12,
        same_site="strict",
        https_only=defs.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    for r in (auth.router, inicio.router, painel.router, movimentos.router, contas.router, importar.router, categorias.router,
              regras.router, relatorios.router, emprestimos.router, previsoes.router, pareaveis.router, viagens.router,
              subscricoes.router, abastecer.router, patrimonio.router, posicoes.router, admin.router, documentos.router,
              saude.router, rotas_tv.router, admin_paperless.router):
        app.include_router(r)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """Para o HEALTHCHECK do container: a app responde e a base lê-se."""
        from sqlalchemy import text
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            return PlainTextResponse("base de dados indisponível", status_code=503)
        return PlainTextResponse("ok")

    @app.exception_handler(NaoAutenticado)
    async def _nao_autenticado(request: Request, _exc):
        destino = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(destino, safe='/')}", status_code=303)

    @app.exception_handler(rotas_tv.NaoEmparelhado)
    async def _tv_nao_emparelhada(request: Request, _exc):
        resp = RedirectResponse("/tv/emparelhar", status_code=303)
        resp.delete_cookie(tv.COOKIE, path="/tv")
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def _erro_http(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api/", "/static/")):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        return render(request, "erro.html", status_code=exc.status_code,
                      codigo=exc.status_code, mensagem=exc.detail)

    return app
