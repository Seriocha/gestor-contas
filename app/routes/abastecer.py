"""Registo de abastecimentos sem login e a sua validação.

- /abastecer/{código}: página simples (telemóvel, sem login, só acessível na rede/VPN e com o código secreto do
  link, gerado em Património › Abastecimentos por validar) para registar um abastecimento de um carro.
  O registo fica pendente: não mexe em nada até ser validado.
- /patrimonio/abastecimentos: quem tem acesso (editar) à entidade do carro valida (podendo corrigir), ou rejeita.
  Validado, passa a abastecimento como os do Fuelio: leitura de km, ligação ao débito no banco, consumo.
"""
import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import fuelio, preferencias
from .. import manutencao as mt
from .. import patrimonio as pt
from ..deps import IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, render, utilizador_atual
from ..formatos import montante_para_input, parse_data, parse_montante
from ..models import PAPEIS, Abastecimento, AbastecimentoPendente, Bem, Utilizador
from ..seguranca import ler_form

router = APIRouter()
MAX_PENDENTES = 300          # trava enchimentos acidentais da página sem login


def _token_valido(request: Request, codigo: str) -> None:
    esperado = request.app.state.prefs.get("abastecer_token") or ""
    if not esperado or not secrets.compare_digest(codigo, esperado):
        raise HTTPException(status_code=404, detail="Página não encontrada.")


def _carros(db: Session) -> list[Bem]:
    return [b for b in db.scalars(select(Bem).where(Bem.tipo == "carro").order_by(Bem.nome)) if not b.vendido]


def _ler(form, erros: list) -> dict:
    """Campos do abastecimento (página pública e validação)."""
    v = {k: str(form.get(k, "")).strip() for k in ("data", "hora", "km", "litros", "valor", "posto", "notas", "quem")}
    try:
        data = parse_data(v["data"]) if v["data"] else date.today()
        if data > date.today() or (date.today() - data).days > 400:
            raise ValueError
    except ValueError:
        data = None
        erros.append("Data inválida.")
    hora = v["hora"][:5] if len(v["hora"]) >= 4 and v["hora"][:2].isdigit() else None
    km_txt = v["km"].replace(" ", "").replace(".", "")
    km = int(km_txt) if km_txt.isdigit() and int(km_txt) < 5_000_000 else None
    if v["km"] and km is None:
        erros.append("Km inválidos.")
    try:
        litros = float(v["litros"].replace(",", "."))
        if not 0 < litros < 500:
            raise ValueError
    except ValueError:
        litros = None
        erros.append("Indica os litros (ex.: 41,04).")
    try:
        valor = abs(parse_montante(v["valor"])) if v["valor"] else None
        if valor is not None and not 0 < valor < 100_000:
            raise ValueError("Valor inválido.")
    except ValueError:
        valor = None
        erros.append("Valor pago inválido (ex.: 84,91).")
    return {"data": data, "hora": hora, "km": km, "litros": litros, "valor": valor, "cheio": bool(form.get("cheio")),
            "posto": v["posto"][:120] or None, "notas": v["notas"][:300] or None, "quem": v["quem"][:60] or None}


# ------------------------------------------------------------------ página sem login
@router.get("/abastecer/{codigo}")
def pagina(request: Request, codigo: str, ok: IntOpcional = None, db: Session = Depends(get_db)):
    _token_valido(request, codigo)
    feito = db.get(AbastecimentoPendente, ok) if ok else None
    return render(request, "abastecer/pagina.html", carros=_carros(db), codigo=codigo, feito=feito, erros=[], v={},
                  agora=datetime.now())


@router.post("/abastecer/{codigo}")
async def registar(request: Request, codigo: str, db: Session = Depends(get_db)):
    _token_valido(request, codigo)
    form = await ler_form(request)
    erros = []
    carros = {b.id: b for b in _carros(db)}
    bid = str(form.get("bem_id", ""))
    b = carros.get(int(bid)) if bid.isdigit() else None
    if not b:
        erros.append("Escolhe o carro.")
    dados = _ler(form, erros)
    if not erros and (db.scalar(select(func.count()).select_from(AbastecimentoPendente)) or 0) >= MAX_PENDENTES:
        erros.append("Há demasiados abastecimentos por validar. Pede a quem gere a app para os validar primeiro.")
    if erros:
        return render(request, "abastecer/pagina.html", status_code=400, carros=list(carros.values()), codigo=codigo,
                      feito=None, erros=erros, v={k: str(form.get(k, "")) for k in form.keys()}, agora=datetime.now())
    p = AbastecimentoPendente(bem_id=b.id, **dados)
    db.add(p)
    db.commit()
    return RedirectResponse(f"/abastecer/{codigo}?ok={p.id}", status_code=303)


# ------------------------------------------------------------------ validação (com login)
def _pendentes(db: Session, entidade_id: int) -> list[AbastecimentoPendente]:
    return db.scalars(select(AbastecimentoPendente).join(Bem).where(Bem.entidade_id == entidade_id)
                      .order_by(AbastecimentoPendente.data, AbastecimentoPendente.hora)).all()


def n_pendentes(db: Session, entidade_id: int) -> int:
    return db.scalar(select(func.count()).select_from(AbastecimentoPendente).join(Bem)
                     .where(Bem.entidade_id == entidade_id)) or 0


@router.get("/patrimonio/abastecimentos")
def validacao(request: Request, entidade: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
              db: Session = Depends(get_db)):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "abastecer/validar.html", entidade=None, visiveis=visiveis)
    pend = _pendentes(db, ent.id)
    ultimo_km = {}
    for p in pend:
        if p.bem_id not in ultimo_km:
            r = mt.ultimo_registo(p.bem)
            ultimo_km[p.bem_id] = r.km if r else None
    codigo = request.app.state.prefs.get("abastecer_token") or ""
    return render(request, "abastecer/validar.html", visiveis=visiveis, entidade=ent, pendentes=pend, ultimo_km=ultimo_km,
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"], link=f"{str(request.base_url).rstrip('/')}/abastecer/{codigo}"
                  if codigo else None, montante_para_input=montante_para_input)


def _pendente(db: Session, u: Utilizador, pid: int) -> AbastecimentoPendente:
    p = db.get(AbastecimentoPendente, pid)
    if not p:
        raise HTTPException(status_code=404, detail="Este abastecimento já foi tratado.")
    exigir_papel(db, u, p.bem.entidade_id, "editar")
    return p


@router.post("/patrimonio/abastecimentos/{pid}/validar")
async def validar(request: Request, pid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    p = _pendente(db, u, pid)
    b, ent_id = p.bem, p.bem.entidade_id
    voltar = f"/patrimonio/abastecimentos?entidade={ent_id}"
    erros = []
    dados = _ler(form, erros)          # o formulário traz os valores (corrigidos ou não)
    if erros:
        flash(request, " ".join(erros), "erro")
        return RedirectResponse(voltar, status_code=303)
    dados.pop("quem")
    notas = dados.pop("notas")
    if p.quem:
        notas = f"{notas} · registado por {p.quem}" if notas else f"registado por {p.quem}"
    a = Abastecimento(bem_id=b.id, guid=f"web:{p.id}", notas=notas, preco_litro=round(dados["valor"] / 100 / dados["litros"], 3)
                      if dados["valor"] else None, **dados)
    db.add(a)
    aviso = ""
    if a.km:
        try:
            mt.registar_km(db, b, a.data, a.km, origem="abastec", nota=a.posto)
        except ValueError as e:
            aviso = f" Km não registados: {e}"
    db.delete(p)
    db.flush()
    ligados = fuelio.ligar(db, ent_id)
    pt.atribuir(db, ent_id)
    auditar(db, u, "abastecimento_validado", f"{b.nome} {a.data} {a.litros} l")
    db.commit()
    flash(request, f"Abastecimento de {b.nome} validado" + (" e ligado ao débito no banco." if ligados else ".") + aviso)
    return RedirectResponse(voltar, status_code=303)


@router.post("/patrimonio/abastecimentos/{pid}/rejeitar")
async def rejeitar(request: Request, pid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    p = _pendente(db, u, pid)
    ent_id = p.bem.entidade_id
    auditar(db, u, "abastecimento_rejeitado", f"{p.bem.nome} {p.data} {p.litros} l")
    db.delete(p)
    db.commit()
    flash(request, "Abastecimento rejeitado.")
    return RedirectResponse(f"/patrimonio/abastecimentos?entidade={ent_id}", status_code=303)


@router.post("/patrimonio/abastecimentos/link")
async def gerir_link(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Só administradores: gera um código novo (o link antigo deixa de funcionar) ou desliga a página."""
    form = await ler_form(request)
    if not u.is_admin:
        raise HTTPException(status_code=403, detail="Só um administrador gere o link.")
    acao = str(form.get("acao", ""))
    preferencias.gravar(db, "abastecer_token", secrets.token_urlsafe(18) if acao == "gerar" else "")
    auditar(db, u, "abastecer_link", acao)
    db.commit()
    request.app.state.prefs = preferencias.carregar(db)
    flash(request, "Novo link gerado — o anterior deixou de funcionar." if acao == "gerar" else "Página de registo desligada.")
    return RedirectResponse(str(form.get("voltar") or "/patrimonio/abastecimentos"), status_code=303)
