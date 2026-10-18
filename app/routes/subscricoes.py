"""Subscrições e assinaturas: lista com custos, próximos pagamentos e fidelização,
ficha com os débitos encontrados, sugestões detetadas nos movimentos."""
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import subscricoes as sb
from ..deps import IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, render, utilizador_atual
from ..formatos import montante_para_input, parse_data, parse_montante
from ..models import PAPEIS, PERIODICIDADES, Categoria, Conta, Subscricao, SubscricaoIgnorada, Utilizador
from ..qualificacao import arvore_categorias, validar_padrao
from ..seguranca import ler_form

router = APIRouter(prefix="/subscricoes")


def _sub(db: Session, u: Utilizador, sid: int, minimo: str = "ver") -> tuple[Subscricao, str]:
    s = db.get(Subscricao, sid)
    if not s:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return s, exigir_papel(db, u, s.entidade_id, minimo)


@router.get("")
def lista(request: Request, entidade: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
          db: Session = Depends(get_db)):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "subscricoes/lista.html", entidade=None, visiveis=visiveis)
    if not ent.tem("subscricoes"):
        return render(request, "_sem_modulo.html", visiveis=visiveis, entidade=ent, titulo="Subscrições",
                      destino="/subscricoes", modulo="subscrições")
    hoje = date.today()
    sugestoes = sb.detetar(db, ent.id, hoje)
    for s in sugestoes:   # link para o formulário já preenchido
        s.href = "/subscricoes/nova?" + urlencode({
            "entidade": ent.id, "nome": s.nome, "padrao": s.padrao, "valor": montante_para_input(s.valor),
            "periodicidade": s.periodicidade, "dia": s.dia, "inicio": s.inicio.isoformat(),
            "conta_id": s.conta_id or "", "categoria_id": s.categoria_id or "", "servico": s.servico or ""})
    return render(request, "subscricoes/lista.html", visiveis=visiveis, entidade=ent, r=sb.resumo(db, ent.id, hoje),
                  sugestoes=sugestoes, periodicidades=PERIODICIDADES, tipos=sb.TIPOS, hoje=hoje,
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


# ------------------------------------------------------------------ formulário
def _form(request, db, ent, v, sub=None, erros=None, status_code=200, analise=None):
    contas = db.scalars(select(Conta).where(Conta.entidade_id == ent.id).order_by(Conta.nome)).all()
    return render(request, "subscricoes/form.html", status_code=status_code, entidade=ent, sub=sub, v=v,
                  erros=erros or [], contas=contas, categorias=arvore_categorias(db), periodicidades=PERIODICIDADES,
                  catalogo=sb.CATALOGO, categorias_por_nome={c.nome: c.id for c in db.scalars(select(Categoria))},
                  analise=analise)


def _ler(form, db: Session, entidade_id: int) -> tuple[dict, list[str], dict]:
    campos = ("nome", "servico", "conta_id", "categoria_id", "valor", "periodicidade", "dia", "inicio",
              "fim_fidelizacao", "fim", "padrao", "notas")
    v = {k: str(form.get(k, "")).strip() for k in campos}
    erros = []
    if not v["nome"]:
        erros.append("Dá um nome à subscrição (ex.: «Netflix», «MEO casa»).")
    try:
        valor = abs(parse_montante(v["valor"])) if v["valor"] else 0
    except ValueError as e:
        valor = 0
        erros.append(str(e))
    if not valor:
        erros.append("Indica o valor de cada pagamento.")
    datas = {}
    for k, rotulo in (("inicio", "início"), ("fim_fidelizacao", "fim da fidelização"), ("fim", "fim")):
        try:
            datas[k] = parse_data(v[k]) if v[k] else None
        except ValueError:
            datas[k] = None
            erros.append(f"Data de {rotulo} inválida.")
    if not datas.get("inicio"):
        erros.append("Indica a data de início (ou a do primeiro pagamento).")
    elif datas.get("fim") and datas["fim"] < datas["inicio"]:
        erros.append("O fim não pode ser antes do início.")
    if v["padrao"]:
        erro = validar_padrao(v["padrao"], "contem")
        if erro:
            erros.append(erro)
    conta = db.get(Conta, int(v["conta_id"])) if v["conta_id"].isdigit() else None
    if conta and conta.entidade_id != entidade_id:
        conta = None
    cat = db.get(Categoria, int(v["categoria_id"])) if v["categoria_id"].isdigit() else None
    dados = {"nome": v["nome"][:100], "servico": v["servico"][:40] or None, "conta_id": conta.id if conta else None,
             "categoria_id": cat.id if cat else None, "valor": valor,
             "periodicidade": v["periodicidade"] if v["periodicidade"] in PERIODICIDADES else "mensal",
             "dia_pagamento": int(v["dia"]) if v["dia"].isdigit() and 1 <= int(v["dia"]) <= 31 else None,
             "data_inicio": datas.get("inicio"), "fim_fidelizacao": datas.get("fim_fidelizacao"),
             "data_fim": datas.get("fim"), "padrao": v["padrao"].upper()[:200] or None, "notas": v["notas"] or None}
    return v, erros, dados


@router.get("/nova")
def nova(request: Request, entidade: int, nome: str = "", padrao: str = "", valor: str = "", periodicidade: str = "mensal",
         dia: str = "", inicio: str = "", conta_id: str = "", categoria_id: str = "", servico: str = "", mov: str = "",
         u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    if mov.isdigit() and not padrao:
        from ..models import Movimento
        from ..sugestoes import padrao_de
        m = db.get(Movimento, int(mov))
        if m and m.conta.entidade_id == entidade:
            padrao = (padrao_de(m.descricao_norm or m.descricao_original or "") or "").upper()
    v = {"nome": nome, "padrao": padrao, "valor": valor, "periodicidade": periodicidade, "dia": dia,
         "inicio": inicio or date.today().isoformat(), "conta_id": conta_id, "categoria_id": categoria_id,
         "servico": servico, "mov": mov}
    return _form(request, db, ent, v)


@router.post("/nova")
async def criar(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    v, erros, dados = _ler(await ler_form(request), db, ent.id)
    if erros:
        return _form(request, db, ent, v, erros=erros, status_code=400)
    s = Subscricao(entidade_id=ent.id, **dados)
    db.add(s)
    mov = request.query_params.get("mov", "")
    if mov.isdigit():
        from ..models import Movimento
        m = db.get(Movimento, int(mov))
        if m and m.conta.entidade_id == ent.id:
            db.flush()
            m.subscricao_id = s.id
    auditar(db, u, "subscricao_criada", s.nome)
    db.commit()
    flash(request, f"Subscrição «{s.nome}» registada.")
    return RedirectResponse(f"/subscricoes/{s.id}", status_code=303)


@router.post("/ignorar")
async def ignorar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    ent_id = int(form["entidade"]) if str(form.get("entidade", "")).isdigit() else 0
    exigir_papel(db, u, ent_id, "editar")
    padrao = str(form.get("padrao", "")).strip()[:200]
    if padrao and not db.scalar(select(SubscricaoIgnorada.id).where(SubscricaoIgnorada.entidade_id == ent_id,
                                                                     SubscricaoIgnorada.padrao == padrao)):
        db.add(SubscricaoIgnorada(entidade_id=ent_id, padrao=padrao))
        db.commit()
    flash(request, f"«{padrao}» não volta a ser sugerido como subscrição.")
    return RedirectResponse(f"/subscricoes?entidade={ent_id}#sugestoes", status_code=303)


@router.post("/movimento/{mid}")
async def associar_movimento(request: Request, mid: int, u: Utilizador = Depends(utilizador_atual),
                             db: Session = Depends(get_db)):
    """Associa (ou desassocia) um movimento a uma subscrição. Se a subscrição ainda não tem texto do extrato,
    fica com o deste movimento — para os débitos seguintes serem reconhecidos sozinhos."""
    from ..models import Movimento
    from ..sugestoes import padrao_de
    form = await ler_form(request)
    m = db.get(Movimento, mid)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, m.conta.entidade_id, "editar")
    sid = str(form.get("subscricao_id", ""))
    s = db.get(Subscricao, int(sid)) if sid.isdigit() else None
    voltar = str(form.get("voltar", "")) or f"/movimentos/{m.id}"
    if not voltar.startswith("/") or voltar.startswith("//"):
        voltar = f"/movimentos/{m.id}"
    if s and s.entidade_id != m.conta.entidade_id:
        raise HTTPException(status_code=400, detail="A subscrição é de outra entidade.")
    m.subscricao_id = s.id if s else None
    msg = "Movimento sem subscrição."
    if s:
        msg = f"Movimento associado a «{s.nome}»."
        if not s.padrao:
            p = padrao_de(m.descricao_norm or m.descricao_original or "")
            if p:
                s.padrao = p.upper()[:200]
                msg += f" Os próximos débitos com «{s.padrao}» passam a contar sozinhos."
        if not m.categoria_id and s.categoria_id and m.qualificado_por != "manual":
            m.categoria_id, m.qualificado_por = s.categoria_id, "regra"
    auditar(db, u, "movimento_subscricao", f"{m.id} → {s.nome if s else '—'}")
    db.commit()
    flash(request, msg)
    return RedirectResponse(voltar, status_code=303)


@router.get("/{sid}")
def ficha(request: Request, sid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    s, papel = _sub(db, u, sid)
    visiveis, ent, _ = entidade_escolhida(request, db, u, s.entidade_id)
    hoje = date.today()
    return render(request, "subscricoes/ficha.html", visiveis=visiveis, entidade=ent, e=sb.estado(db, s, hoje),
                  periodicidades=PERIODICIDADES, hoje=hoje, pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


@router.get("/{sid}/editar")
def editar(request: Request, sid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    s, _ = _sub(db, u, sid, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, s.entidade_id)
    iso = lambda d: d.isoformat() if d else ""
    v = {"nome": s.nome, "servico": s.servico or "", "conta_id": str(s.conta_id or ""), "categoria_id": str(s.categoria_id or ""),
         "valor": montante_para_input(s.valor), "periodicidade": s.periodicidade, "dia": str(s.dia_pagamento or ""),
         "inicio": iso(s.data_inicio), "fim_fidelizacao": iso(s.fim_fidelizacao), "fim": iso(s.data_fim),
         "padrao": s.padrao or "", "notas": s.notas or ""}
    return _form(request, db, ent, v, sub=s)


@router.post("/{sid}/editar")
async def guardar(request: Request, sid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    s, _ = _sub(db, u, sid, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, s.entidade_id)
    form = await ler_form(request)
    v, erros, dados = _ler(form, db, ent.id)
    if erros:
        return _form(request, db, ent, v, sub=s, erros=erros, status_code=400)
    for k, val in dados.items():
        setattr(s, k, val)
    # débitos assinalados no «Refrescar» para contarem nesta subscrição (associação à mão)
    from ..models import Movimento
    n = 0
    for mid in form.getlist("associar"):
        m = db.get(Movimento, int(mid)) if str(mid).isdigit() else None
        if m and m.conta.entidade_id == s.entidade_id and m.montante < 0:
            m.subscricao_id, n = s.id, n + 1
    auditar(db, u, "subscricao_editada", s.nome + (f" (+{n} débitos associados)" if n else ""))
    db.commit()
    flash(request, "Subscrição guardada." + (f" {n} débito{'s' if n != 1 else ''} associado{'s' if n != 1 else ''}." if n else ""))
    return RedirectResponse(f"/subscricoes/{s.id}", status_code=303)


@router.post("/{sid}/refrescar")
async def refrescar(request: Request, sid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Mostra, sem guardar, que débitos contam com os dados do formulário e os parecidos que ficam de fora (e porquê)."""
    s, _ = _sub(db, u, sid, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, s.entidade_id)
    form = await ler_form(request)
    v, erros, dados = _ler(form, db, ent.id)
    if erros:
        return _form(request, db, ent, v, sub=s, erros=erros, status_code=400)
    provisoria = Subscricao(id=s.id, entidade_id=s.entidade_id, **dados)     # não entra na sessão: nada é gravado
    v["associar"] = [str(x) for x in form.getlist("associar")]
    return _form(request, db, ent, v, sub=s, analise=sb.analisar(db, provisoria, date.today()))


@router.post("/{sid}/apagar")
async def apagar(request: Request, sid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    s, _ = _sub(db, u, sid, "editar")
    ent_id, nome = s.entidade_id, s.nome
    db.delete(s)
    auditar(db, u, "subscricao_apagada", nome)
    db.commit()
    flash(request, f"Subscrição «{nome}» apagada (os movimentos ficam).")
    return RedirectResponse(f"/subscricoes?entidade={ent_id}", status_code=303)
