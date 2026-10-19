import asyncio
import contextlib
from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import backup, formatos, pasta_importacao, preferencias, tarefas
from . import patrimonio as pt
from .config import BASE_DIR, carregar_definicoes
from .db import criar_engine, url_sqlite
from .deps import NaoAutenticado, render
from .routes import (abastecer, admin, auth, documentos, inicio, posicoes, categorias, contas, emprestimos, importar, movimentos, painel, pareaveis, previsoes,
                     patrimonio, regras, relatorios, subscricoes, viagens)
from .seed import semear

APP_DIR = Path(__file__).resolve().parent
VERSAO = "0.60.1"


def aplicar_migracoes(db_path: Path) -> None:
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url_sqlite(db_path))
    command.upgrade(cfg, "head")


def create_app(data_dir: Path | None = None, backups_automaticos: bool = True) -> FastAPI:
    defs = carregar_definicoes(data_dir)
    aplicar_migracoes(defs.db_path)
    engine = criar_engine(defs.db_path)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as db:
        semear(db)
        prefs = preferencias.carregar(db)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tarefas_fundo = [asyncio.create_task(backup.ciclo_backups(defs)),
                         asyncio.create_task(pt.ciclo_revisoes(SessionLocal, lambda: app.state.prefs)),
                         asyncio.create_task(pasta_importacao.ciclo(SessionLocal, defs, lambda: app.state.prefs))] \
            if backups_automaticos else []
        yield
        for t in tarefas_fundo:
            t.cancel()

    app = FastAPI(title="Gestor de Contas", version=VERSAO, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.defs = defs
    app.state.SessionLocal = SessionLocal
    app.state.bloqueios = auth.Bloqueios()
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
              subscricoes.router, abastecer.router, patrimonio.router, posicoes.router, admin.router, documentos.router):
        app.include_router(r)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return PlainTextResponse("ok")

    @app.exception_handler(NaoAutenticado)
    async def _nao_autenticado(request: Request, _exc):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _erro_http(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api/", "/static/")):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        return render(request, "erro.html", status_code=exc.status_code,
                      codigo=exc.status_code, mensagem=exc.detail)

    return app
