"""Património: casas e carros — valor (histórico de avaliações, IA), dívida associada,
despesas por bem (casa principal vs. férias, cada carro) e atribuição das despesas."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import ia
from .. import patrimonio as pt
from .. import relatorios as rel
from ..deps import IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, render, utilizador_atual
from ..formatos import fmt_montante, montante_para_input, parse_data, parse_montante
from ..models import (COMBUSTIVEIS, ESTADOS_BEM, PAPEIS, TIPOS_BEM, USOS_BEM, AvaliacaoBem, Bem, Conta, Movimento,
                      Utilizador)
from ..qualificacao import validar_padrao
from ..seguranca import ler_form

router = APIRouter(prefix="/patrimonio")


def _bem(db: Session, u: Utilizador, bid: int, minimo: str = "ver") -> tuple[Bem, str]:
    b = db.get(Bem, bid)
    if not b:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return b, exigir_papel(db, u, b.entidade_id, minimo)


@router.get("")
def lista(request: Request, entidade: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
          db: Session = Depends(get_db)):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "patrimonio/lista.html", entidade=None, visiveis=visiveis)
    hoje = date.today()
    return render(request, "patrimonio/lista.html", visiveis=visiveis, entidade=ent, r=pt.resumo(db, ent.id, hoje),
                  usos=USOS_BEM, hoje=hoje, ia_disponivel=ia.disponivel(),
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


@router.post("/atribuir")
async def atribuir(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    ent_id = int(form["entidade"]) if str(form.get("entidade", "")).isdigit() else 0
    exigir_papel(db, u, ent_id, "editar")
    n = pt.atribuir(db, ent_id)
    auditar(db, u, "bens_atribuidos", f"{n} movimentos")
    db.commit()
    flash(request, f"{n} movimento{'s' if n != 1 else ''} atribuído{'s' if n != 1 else ''} aos bens." if n
          else "Nenhum movimento novo para atribuir (vê os padrões e o bem «por omissão» de cada tipo).")
    return RedirectResponse(str(form.get("voltar") or f"/patrimonio?entidade={ent_id}"), status_code=303)


# ------------------------------------------------------------------ formulário
CAMPOS = ("nome", "uso", "tipologia", "area_m2", "localizacao", "ano", "estado", "extras", "marca", "modelo", "km",
          "combustivel", "data_aquisicao", "valor_aquisicao", "emprestimo_conta_id", "data_venda", "valor_venda",
          "revisao_meses", "padroes", "omissao", "notas")


def _form(request, db, ent, tipo, v, bem=None, erros=None, status_code=200):
    emprestimos = db.scalars(select(Conta).where(Conta.entidade_id == ent.id, Conta.tipo == "EMPRESTIMO")
                             .order_by(Conta.nome)).all()
    return render(request, "patrimonio/form.html", status_code=status_code, entidade=ent, tipo=tipo, v=v, bem=bem,
                  erros=erros or [], usos=USOS_BEM[tipo], estados=ESTADOS_BEM, combustiveis=COMBUSTIVEIS,
                  emprestimos=emprestimos, tipo_rotulo=TIPOS_BEM[tipo])


def _inteiro(v: str, rotulo: str, erros: list, minimo: int = 0, maximo: int = 10**9) -> int | None:
    v = v.replace(" ", "").replace(".", "") if v else v
    if not v:
        return None
    if not v.isdigit() or not minimo <= int(v) <= maximo:
        erros.append(f"{rotulo} inválido.")
        return None
    return int(v)


def _ler(form, db: Session, entidade_id: int, tipo: str) -> tuple[dict, list[str], dict]:
    v = {k: str(form.get(k, "")).strip() for k in CAMPOS}
    v["omissao"] = "1" if form.get("omissao") else ""
    erros = []
    if not v["nome"]:
        erros.append("Dá um nome ao bem (ex.: «Casa de Lisboa», «Casa da praia», «Golf»).")
    t = v["tipologia"].upper().lstrip("T")
    dados = {
        "nome": v["nome"][:100], "uso": v["uso"] if v["uso"] in USOS_BEM[tipo] else "principal",
        "tipologia": _inteiro(t, "Tipologia (n.º de assoalhadas)", erros, 0, 20),
        "area_m2": _inteiro(v["area_m2"], "Área", erros, 1, 100000),
        "localizacao": v["localizacao"][:200] or None,
        "ano": _inteiro(v["ano"], "Ano", erros, 1500, date.today().year + 2),
        "estado": v["estado"] if v["estado"] in ESTADOS_BEM else None, "extras": v["extras"][:200] or None,
        "marca": v["marca"][:60] or None, "modelo": v["modelo"][:60] or None,
        "km": _inteiro(v["km"], "Quilómetros", erros, 0, 5_000_000),
        "combustivel": v["combustivel"] if v["combustivel"] in COMBUSTIVEIS else None,
        "revisao_meses": _inteiro(v["revisao_meses"], "Periodicidade da revisão", erros, 0, 120) or 0,
        "padroes": "\n".join(p.strip().upper() for p in v["padroes"].splitlines() if p.strip())[:2000] or None,
        "omissao": bool(v["omissao"]), "notas": v["notas"] or None,
    }
    for k, rotulo in (("data_aquisicao", "aquisição"), ("data_venda", "venda")):
        try:
            dados[k] = parse_data(v[k]) if v[k] else None
        except ValueError:
            dados[k] = None
            erros.append(f"Data de {rotulo} inválida.")
    for k, rotulo in (("valor_aquisicao", "Valor de compra"), ("valor_venda", "Valor de venda")):
        try:
            dados[k] = abs(parse_montante(v[k])) if v[k] else None
        except ValueError as e:
            dados[k] = None
            erros.append(f"{rotulo}: {e}")
    for p in (dados["padroes"] or "").splitlines():
        erro = validar_padrao(p, "contem")
        if erro:
            erros.append(f"Padrão «{p}»: {erro}")
    conta = db.get(Conta, int(v["emprestimo_conta_id"])) if v["emprestimo_conta_id"].isdigit() else None
    dados["emprestimo_conta_id"] = conta.id if conta and conta.entidade_id == entidade_id else None
    return v, erros, dados


def _um_por_omissao(db: Session, b: Bem) -> None:
    """Só um bem de cada tipo fica com as despesas «por omissão»."""
    if b.omissao:
        db.execute(update(Bem).where(Bem.entidade_id == b.entidade_id, Bem.tipo == b.tipo, Bem.id != b.id)
                   .values(omissao=False))


@router.get("/novo")
def novo(request: Request, entidade: int, tipo: str = "casa", u: Utilizador = Depends(utilizador_atual),
         db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    tipo = tipo if tipo in TIPOS_BEM else "casa"
    ja_ha = db.scalar(select(Bem.id).where(Bem.entidade_id == ent.id, Bem.tipo == tipo, Bem.omissao))
    v = {"uso": "principal", "revisao_meses": "6" if tipo == "casa" else "12", "omissao": "" if ja_ha else "1"}
    return _form(request, db, ent, tipo, v)


@router.post("/novo")
async def criar(request: Request, entidade: int, tipo: str = "casa", u: Utilizador = Depends(utilizador_atual),
                db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    tipo = tipo if tipo in TIPOS_BEM else "casa"
    v, erros, dados = _ler(await ler_form(request), db, ent.id, tipo)
    if erros:
        return _form(request, db, ent, tipo, v, erros=erros, status_code=400)
    b = Bem(entidade_id=ent.id, tipo=tipo, **dados)
    db.add(b)
    db.flush()
    _um_por_omissao(db, b)
    n = pt.atribuir(db, ent.id)
    auditar(db, u, "bem_criado", b.nome)
    db.commit()
    flash(request, f"«{b.nome}» registado." + (f" {n} movimentos atribuídos." if n else ""))
    return RedirectResponse(f"/patrimonio/{b.id}", status_code=303)


@router.post("/movimento/{mid}")
async def bem_do_movimento(request: Request, mid: int, u: Utilizador = Depends(utilizador_atual),
                           db: Session = Depends(get_db)):
    """Escolher (ou retirar) o bem de um movimento, na página do movimento."""
    form = await ler_form(request)
    m = db.get(Movimento, mid)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, m.conta.entidade_id, "editar")
    bid = str(form.get("bem_id", ""))
    b = db.get(Bem, int(bid)) if bid.isdigit() else None
    m.bem_id, m.bem_manual = (b.id if b and b.entidade_id == m.conta.entidade_id else None), True
    db.commit()
    flash(request, f"Movimento atribuído a «{b.nome}»." if m.bem_id else "Movimento sem bem.")
    voltar = str(form.get("voltar", ""))
    return RedirectResponse(voltar if voltar.startswith("/") and not voltar.startswith("//") else f"/movimentos/{mid}",
                            status_code=303)


@router.get("/{bid}")
def ficha(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    b, papel = _bem(db, u, bid)
    visiveis, ent, _ = entidade_escolhida(request, db, u, b.entidade_id)
    hoje = date.today()
    ini = rel.somar_meses(hoje.replace(day=1), -11)
    valor, divida = pt.valor_atual(b), pt.divida(db, b, hoje)
    return render(request, "patrimonio/ficha.html", visiveis=visiveis, entidade=ent, b=b, hoje=hoje, ini=ini,
                  valor=valor, divida=divida, c=pt.custos(db, b, ini, hoje), proxima=pt.proxima_revisao(b),
                  pendente=pt.revisao_pendente(b, hoje), falta=pt.falta_para_ia(b), ia_disponivel=ia.disponivel(),
                  descricao_ia=pt.descricao_para_ia(b), estados=ESTADOS_BEM, combustiveis=COMBUSTIVEIS,
                  graficos={"bem": pt.serie_valor(b)}, pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


@router.post("/{bid}/avaliar")
async def avaliar(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    b, _ = _bem(db, u, bid, "editar")
    try:
        a = pt.avaliar_ia(db, b, date.today())
    except ia.ErroIA as e:
        db.rollback()
        flash(request, f"Não foi possível avaliar: {e}", "erro")
        return RedirectResponse(f"/patrimonio/{b.id}", status_code=303)
    auditar(db, u, "bem_avaliado", f"{b.nome}: {a.valor} (IA)")
    db.commit()
    flash(request, f"Valor estimado pela IA: {fmt_montante(a.valor)}.")
    return RedirectResponse(f"/patrimonio/{b.id}#avaliacoes", status_code=303)


@router.post("/{bid}/avaliacao")
async def avaliacao_manual(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual),
                           db: Session = Depends(get_db)):
    form = await ler_form(request)
    b, _ = _bem(db, u, bid, "editar")
    try:
        valor = abs(parse_montante(str(form.get("valor", ""))))
        data = parse_data(str(form.get("data", ""))) if form.get("data") else date.today()
    except ValueError as e:
        flash(request, f"Avaliação inválida: {e}", "erro")
        return RedirectResponse(f"/patrimonio/{b.id}#avaliacoes", status_code=303)
    if not valor:
        flash(request, "Indica o valor.", "erro")
        return RedirectResponse(f"/patrimonio/{b.id}#avaliacoes", status_code=303)
    a = AvaliacaoBem(bem_id=b.id, data=data, valor=valor, fonte="manual",
                     justificacao=str(form.get("nota", "")).strip()[:500] or None)
    db.add(a)
    db.flush()
    db.refresh(b)
    auditar(db, u, "bem_avaliado", f"{b.nome}: {valor} (manual)")
    db.commit()
    flash(request, f"Avaliação de {fmt_montante(valor)} em {data.strftime('%d/%m/%Y')} registada.")
    return RedirectResponse(f"/patrimonio/{b.id}#avaliacoes", status_code=303)


@router.post("/{bid}/avaliacoes/{aid}/apagar")
async def apagar_avaliacao(request: Request, bid: int, aid: int, u: Utilizador = Depends(utilizador_atual),
                           db: Session = Depends(get_db)):
    await ler_form(request)
    b, _ = _bem(db, u, bid, "editar")
    a = db.get(AvaliacaoBem, aid)
    if not a or a.bem_id != b.id:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    db.delete(a)
    db.commit()
    flash(request, "Avaliação apagada.")
    return RedirectResponse(f"/patrimonio/{b.id}#avaliacoes", status_code=303)


@router.get("/{bid}/editar")
def editar(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    b, _ = _bem(db, u, bid, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, b.entidade_id)
    iso = lambda d: d.isoformat() if d else ""
    txt = lambda x: "" if x is None else str(x)
    v = {k: txt(getattr(b, k)) for k in CAMPOS}
    v.update({"data_aquisicao": iso(b.data_aquisicao), "data_venda": iso(b.data_venda),
              "valor_aquisicao": montante_para_input(b.valor_aquisicao) if b.valor_aquisicao else "",
              "valor_venda": montante_para_input(b.valor_venda) if b.valor_venda else "",
              "omissao": "1" if b.omissao else "", "padroes": b.padroes or "", "notas": b.notas or ""})
    return _form(request, db, ent, b.tipo, v, bem=b)


@router.post("/{bid}/editar")
async def guardar(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    b, _ = _bem(db, u, bid, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, b.entidade_id)
    v, erros, dados = _ler(await ler_form(request), db, ent.id, b.tipo)
    if erros:
        return _form(request, db, ent, b.tipo, v, bem=b, erros=erros, status_code=400)
    for k, val in dados.items():
        setattr(b, k, val)
    _um_por_omissao(db, b)
    n = pt.atribuir(db, ent.id)
    auditar(db, u, "bem_editado", b.nome)
    db.commit()
    flash(request, "Guardado." + (f" {n} movimentos atribuídos." if n else ""))
    return RedirectResponse(f"/patrimonio/{b.id}", status_code=303)


@router.post("/{bid}/apagar")
async def apagar(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    b, _ = _bem(db, u, bid, "editar")
    ent_id, nome = b.entidade_id, b.nome
    db.execute(update(Movimento).where(Movimento.bem_id == b.id).values(bem_id=None, bem_manual=False))
    db.delete(b)
    auditar(db, u, "bem_apagado", nome)
    db.commit()
    flash(request, f"«{nome}» apagado (os movimentos ficam, sem bem).")
    return RedirectResponse(f"/patrimonio?entidade={ent_id}", status_code=303)
