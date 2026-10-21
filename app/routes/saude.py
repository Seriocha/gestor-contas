"""Saúde: perfis por pessoa (na entidade), importação do Google Health (Takeout) e evolução em gráficos.
Quem tem acesso à entidade vê os perfis dela; importar e gerir perfis pede «editar»."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import saude as sd
from ..deps import IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, render, utilizador_atual
from ..models import PAPEIS, MedidaSaude, Permissao, PerfilSaude, TreinoSaude, Utilizador
from ..seguranca import ler_form

router = APIRouter(prefix="/saude")


def _perfil(db: Session, u: Utilizador, pid: int, minimo: str = "ver") -> PerfilSaude:
    p = db.get(PerfilSaude, pid)
    if not p:
        raise HTTPException(status_code=404, detail="Perfil não encontrado.")
    exigir_papel(db, u, p.entidade_id, minimo)
    return p


@router.get("")
def pagina(request: Request, entidade: IntOpcional = None, perfil: IntOpcional = None, periodo: str = "90",
           u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "saude/index.html", entidade=None, visiveis=visiveis)
    if not ent.tem("saude"):
        return render(request, "_sem_modulo.html", visiveis=visiveis, entidade=ent, titulo="Saúde", destino="/saude",
                      modulo="saúde")
    perfis = db.scalars(select(PerfilSaude).where(PerfilSaude.entidade_id == ent.id).order_by(PerfilSaude.nome)).all()
    # o perfil escolhido; senão o do próprio utilizador; senão o primeiro
    atual = next((p for p in perfis if p.id == perfil), None) or next((p for p in perfis if p.utilizador_id == u.id), None) \
        or (perfis[0] if perfis else None)
    periodo = periodo if periodo in sd.PERIODOS else "90"
    r = sd.resumo(db, atual, periodo) if atual else None
    contagens = dict(db.execute(select(MedidaSaude.perfil_id, func.count()).group_by(MedidaSaude.perfil_id)).all())
    # para ligar um perfil a uma conta: só quem tem acesso a esta entidade
    utilizadores = db.scalars(select(Utilizador).join(Permissao, Permissao.utilizador_id == Utilizador.id)
                              .where(Utilizador.ativo, Permissao.entidade_id == ent.id).order_by(Utilizador.nome)).all()
    return render(request, "saude/index.html", visiveis=visiveis, entidade=ent, perfis=perfis, atual=atual, r=r,
                  periodo=periodo, periodos=sd.PERIODOS, tipos=sd.TIPOS, contagens=contagens, utilizadores=utilizadores,
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


def _destino(p: PerfilSaude, ancora: str = "") -> RedirectResponse:
    return RedirectResponse(f"/saude?entidade={p.entidade_id}&perfil={p.id}{ancora}", status_code=303)


def _utilizador(form) -> int | None:
    v = str(form.get("utilizador_id", ""))
    return int(v) if v.isdigit() else None


@router.post("/perfis")
async def criar_perfil(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    ent_id = int(form["entidade"]) if str(form.get("entidade", "")).isdigit() else 0
    exigir_papel(db, u, ent_id, "editar")
    nome = str(form.get("nome", "")).strip()[:80]
    if not nome:
        flash(request, "Dá um nome ao perfil (ex.: o nome da pessoa).", "erro")
        return RedirectResponse(f"/saude?entidade={ent_id}", status_code=303)
    p = PerfilSaude(entidade_id=ent_id, nome=nome, utilizador_id=_utilizador(form))
    db.add(p)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        flash(request, f"Já existe um perfil «{nome}».", "erro")
        return RedirectResponse(f"/saude?entidade={ent_id}", status_code=303)
    auditar(db, u, "saude_perfil", f"criado: {nome}")
    db.commit()
    flash(request, f"Perfil «{nome}» criado. Importa agora a exportação do Google Health.")
    return _destino(p, "#importar")


@router.post("/perfis/{pid}/editar")
async def editar_perfil(request: Request, pid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    p = _perfil(db, u, pid, "editar")
    form = await ler_form(request)
    nome = str(form.get("nome", "")).strip()[:80]
    if nome:
        p.nome = nome
    p.utilizador_id = _utilizador(form)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, f"Já existe um perfil «{nome}».", "erro")
        return _destino(p)
    flash(request, "Perfil guardado.")
    return _destino(p)


@router.post("/perfis/{pid}/apagar")
async def apagar_perfil(request: Request, pid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    p = _perfil(db, u, pid, "editar")
    await ler_form(request)
    ent_id, nome = p.entidade_id, p.nome
    db.delete(p)
    auditar(db, u, "saude_perfil", f"apagado: {nome}")
    db.commit()
    flash(request, f"Perfil «{nome}» apagado, com todos os dados de saúde.")
    return RedirectResponse(f"/saude?entidade={ent_id}", status_code=303)


@router.post("/perfis/{pid}/importar")
async def importar(request: Request, pid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """O .zip do Google Takeout (Google Health). Pode ter centenas de MB: o Starlette guarda-o em disco temporário e
    só se leem as pastas que interessam."""
    p = _perfil(db, u, pid, "editar")
    form = await ler_form(request)
    f = form.get("ficheiro")
    if not getattr(f, "filename", ""):
        flash(request, "Escolhe o .zip exportado do Google Takeout.", "erro")
        return _destino(p, "#importar")
    try:
        leitura = await asyncio.to_thread(sd.ler_takeout, f.file)
    except sd.ErroSaude as e:
        flash(request, str(e), "erro")
        return _destino(p, "#importar")
    if not leitura.medidas and not leitura.treinos:
        flash(request, "O ficheiro não tem dados de saúde que a app conheça.", "erro")
        return _destino(p, "#importar")
    res = sd.gravar(db, p, leitura)
    dias = leitura.dias
    # só contagens: nada de valores de saúde nos registos
    auditar(db, u, "saude_importacao", f"{p.nome}: {len(dias)} dias, {res['novas']} novas, {res['alteradas']} alteradas, "
                                        f"{res['treinos']} treinos")
    db.commit()
    flash(request, f"Importados {len(dias)} dias ({dias[0]:%d/%m/%Y} a {dias[-1]:%d/%m/%Y}): {res['novas']} valores novos, "
                   f"{res['alteradas']} atualizados, {res['treinos']} treinos novos." if dias else
          f"Importados {res['treinos']} treinos novos.")
    return _destino(p)
