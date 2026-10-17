"""Património: casas e carros — valor (histórico de avaliações, IA), dívida associada,
despesas por bem (casa principal vs. férias, cada carro) e atribuição das despesas."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import ia
from .. import manutencao as mt
from .. import fuelio
from .. import viaverde as vv
from .. import patrimonio as pt
from .. import relatorios as rel
from ..deps import IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, render, utilizador_atual
from ..formatos import fmt_montante, montante_para_input, parse_data, parse_montante
from ..models import (TIPOS_CONSTRUCAO, Construcao, ACESSOS, AGUAS, COMBUSTIVEIS, ESTADOS_BEM, OCUPACOES, PAPEIS, TIPOS_BEM, USOS_BEM, AvaliacaoBem, Bem, Conta, Manutencao,
                      Movimento, RegistoKm, Utilizador, norm_matricula)
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
                  vvr=vv.resumo(db, ent.id), por_validar=_n_por_validar(db, ent.id),
                  usos=USOS_BEM, hoje=hoje, ia_disponivel=ia.disponivel(),
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


def _n_por_validar(db: Session, entidade_id: int) -> int:
    from .abastecer import n_pendentes
    return n_pendentes(db, entidade_id)


@router.post("/via-verde")
async def importar_via_verde(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Extratos Via Verde (CSV ou ZIP, vários de uma vez): grava as linhas e liga-as aos débitos do banco."""
    form = await ler_form(request)
    ent_id = int(form["entidade"]) if str(form.get("entidade", "")).isdigit() else 0
    exigir_papel(db, u, ent_id, "editar")
    voltar = f"/patrimonio?entidade={ent_id}#via-verde"
    ficheiros = [f for f in form.getlist("ficheiros") if getattr(f, "filename", "")]
    if not ficheiros:
        flash(request, "Escolhe os ficheiros do extrato Via Verde (CSV ou ZIP).", "erro")
        return RedirectResponse(voltar, status_code=303)
    ext = vv.Extrato()
    try:
        for f in ficheiros:
            conteudo = await f.read(5 * 1024 * 1024 + 1)
            if len(conteudo) > 5 * 1024 * 1024:
                raise ValueError(f"«{f.filename}» é demasiado grande.")
            vv.ler(f.filename, conteudo, ext)
    except Exception as e:   # zip/csv corrompido ou de outro tipo
        flash(request, f"Não foi possível ler: {e}", "erro")
        return RedirectResponse(voltar, status_code=303)
    novas, rep = vv.importar(db, ent_id, ext)
    ligados = vv.ligar(db, ent_id)
    auditar(db, u, "via_verde", f"{ext.ficheiros} ficheiros, {novas} linhas novas, {ligados} débitos ligados")
    db.commit()
    flash(request, f"Via Verde: {novas} linha{'s' if novas != 1 else ''} nova{'s' if novas != 1 else ''} de {ext.ficheiros} "
                   f"extrato{'s' if ext.ficheiros != 1 else ''}" + (f" ({rep} já existiam)" if rep else "")
                   + f"; {ligados} débito{'s' if ligados != 1 else ''} do banco ligado{'s' if ligados != 1 else ''}.")
    return RedirectResponse(voltar, status_code=303)


@router.post("/atribuir")
async def atribuir(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    ent_id = int(form["entidade"]) if str(form.get("entidade", "")).isdigit() else 0
    exigir_papel(db, u, ent_id, "editar")
    n = pt.atribuir(db, ent_id)
    vv.ligar(db, ent_id)
    auditar(db, u, "bens_atribuidos", f"{n} movimentos")
    db.commit()
    flash(request, f"{n} movimento{'s' if n != 1 else ''} atribuído{'s' if n != 1 else ''} aos bens." if n
          else "Nenhum movimento novo para atribuir (vê os padrões e o bem «por omissão» de cada tipo).")
    return RedirectResponse(str(form.get("voltar") or f"/patrimonio?entidade={ent_id}"), status_code=303)


# ------------------------------------------------------------------ formulário
CAMPOS = ("nome", "uso", "tipologia", "area_m2", "localizacao", "ano", "estado", "extras", "marca", "modelo", "km",
          "combustivel", "data_aquisicao", "valor_aquisicao", "emprestimo_conta_id", "data_venda", "valor_venda",
          "revisao_meses", "padroes", "omissao", "notas", "matricula", "area_ha", "artigo", "acesso", "anos_tiragem",
          "sobreiros", "producao_arrobas")


def _form(request, db, ent, tipo, v, bem=None, erros=None, status_code=200):
    emprestimos = db.scalars(select(Conta).where(Conta.entidade_id == ent.id, Conta.tipo == "EMPRESTIMO")
                             .order_by(Conta.nome)).all()
    return render(request, "patrimonio/form.html", status_code=status_code, entidade=ent, tipo=tipo, v=v, bem=bem,
                  erros=erros or [], usos=USOS_BEM[tipo], estados=ESTADOS_BEM, combustiveis=COMBUSTIVEIS,
                  emprestimos=emprestimos, tipo_rotulo=TIPOS_BEM[tipo], ocupacoes=OCUPACOES, aguas=AGUAS,
                  acessos=ACESSOS)


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
    v["tem_cortica"] = "1" if form.get("tem_cortica") else ""
    v["ocupacao"] = [x for x in form.getlist("ocupacao") if x in OCUPACOES]
    v["agua"] = [x for x in form.getlist("agua") if x in AGUAS]
    erros = []
    if not v["nome"]:
        erros.append("Dá um nome ao bem (ex.: «Casa de Lisboa», «Casa da praia», «Golf»).")
    t = v["tipologia"].upper().lstrip("T")
    dados = {
        "nome": v["nome"][:100], "uso": v["uso"] if v["uso"] in USOS_BEM[tipo] else next(iter(USOS_BEM[tipo])),
        "tipologia": _inteiro(t, "Tipologia (n.º de assoalhadas)", erros, 0, 20),
        "area_m2": _inteiro(v["area_m2"], "Área", erros, 1, 100000),
        "localizacao": v["localizacao"][:200] or None,
        "ano": _inteiro(v["ano"], "Ano", erros, 1500, date.today().year + 2),
        "estado": v["estado"] if v["estado"] in ESTADOS_BEM else None, "extras": v["extras"][:200] or None,
        "marca": v["marca"][:60] or None, "modelo": v["modelo"][:60] or None,
        "km": _inteiro(v["km"], "Quilómetros", erros, 0, 5_000_000),
        "combustivel": v["combustivel"] if v["combustivel"] in COMBUSTIVEIS else None,
        "matricula": v["matricula"].upper()[:12] or None if tipo == "carro" else None,
        "revisao_meses": _inteiro(v["revisao_meses"], "Periodicidade da revisão", erros, 0, 120) or 0,
        "padroes": "\n".join(p.strip().upper() for p in v["padroes"].splitlines() if p.strip())[:2000] or None,
        "omissao": bool(v["omissao"]) and tipo != "rustico", "notas": v["notas"] or None,
    }
    if tipo == "rustico":
        try:
            area = float(v["area_ha"].replace(" ", "").replace(",", ".")) if v["area_ha"] else None
            if area is not None and not 0 < area < 100000:
                raise ValueError
        except ValueError:
            area = None
            erros.append("Área (hectares) inválida.")
        anos = [x for x in v["anos_tiragem"].replace(";", ",").replace(" ", ",").split(",") if x.strip()]
        if any(not (a.isdigit() and 1900 < int(a) <= date.today().year + 1) for a in anos):
            erros.append("Anos de tiragem inválidos (ex.: 2008, 2017, 2026).")
        anos = sorted({int(a) for a in anos if a.isdigit()})
        dados.update(area_ha=area, artigo=v["artigo"][:60] or None, ocupacao=",".join(v["ocupacao"]) or None,
                     agua=",".join(v["agua"]) or None, acesso=v["acesso"] if v["acesso"] in ACESSOS else None,
                     tem_cortica=bool(v["tem_cortica"]), anos_tiragem=", ".join(map(str, anos)) or None,
                     sobreiros=_inteiro(v["sobreiros"], "N.º de sobreiros", erros, 0, 10_000_000),
                     producao_arrobas=_inteiro(v["producao_arrobas"], "Produção (arrobas)", erros, 0, 10_000_000))
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
    if dados["matricula"] and len(norm_matricula(dados["matricula"])) not in (6, 7, 8):
        erros.append("Matrícula inválida (ex.: AA-00-AA).")
    for p in (dados["padroes"] or "").splitlines():
        erro = validar_padrao(p, "contem")
        if erro:
            erros.append(f"Padrão «{p}»: {erro}")
    conta = db.get(Conta, int(v["emprestimo_conta_id"])) if v["emprestimo_conta_id"].isdigit() else None
    dados["emprestimo_conta_id"] = conta.id if conta and conta.entidade_id == entidade_id else None
    if tipo == "carro" and dados["uso"] == "empresa":
        # Não é património: só as datas (período em que a usas) contam, para alocar o combustível.
        dados.update(valor_aquisicao=None, valor_venda=None, emprestimo_conta_id=None, revisao_meses=0)
    return v, erros, dados


def _km_da_ficha(db: Session, b: Bem) -> None:
    """Os km escritos no formulário do carro entram como leitura de hoje (se mudaram)."""
    if b.tipo != "carro" or b.km is None:
        return
    ultimo = mt.ultimo_registo(b)
    if ultimo is None or ultimo.km != b.km:
        try:
            mt.registar_km(db, b, date.today(), b.km, origem="ficha")
        except ValueError:
            if ultimo:
                b.km = ultimo.km


def _um_por_omissao(db: Session, b: Bem) -> None:
    """Só um bem de cada grupo (casa, carro, viatura da empresa) fica com as despesas «por omissão»."""
    if b.omissao:
        for o in db.scalars(select(Bem).where(Bem.entidade_id == b.entidade_id, Bem.tipo == b.tipo,
                                              Bem.id != b.id, Bem.omissao)):
            if pt.grupo_omissao(o) == pt.grupo_omissao(b):
                o.omissao = False


@router.get("/novo")
def novo(request: Request, entidade: int, tipo: str = "casa", uso: str = "principal",
         u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    tipo = tipo if tipo in TIPOS_BEM else "casa"
    uso = uso if uso in USOS_BEM[tipo] else next(iter(USOS_BEM[tipo]))
    grupo = "empresa" if tipo == "carro" and uso == "empresa" else tipo
    ja_ha = any(pt.grupo_omissao(b) == grupo for b in db.scalars(select(Bem).where(
        Bem.entidade_id == ent.id, Bem.tipo == tipo, Bem.omissao)))
    v = {"uso": uso, "revisao_meses": "0" if grupo == "empresa" else "6" if tipo == "casa" else "12",
         "ocupacao": [], "agua": [],
         "omissao": "" if ja_ha else "1"}
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
    _km_da_ficha(db, b)
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
                  ocupacoes=OCUPACOES, aguas=AGUAS, acessos=ACESSOS, tipos_construcao=TIPOS_CONSTRUCAO,
                  construcoes=pt.construcoes_por_artigo(b) if b.tipo == "rustico" else None,
                  graficos={"bem": pt.serie_valor(b)}, pode_editar=PAPEIS[papel] >= PAPEIS["editar"],
                  mr=mt.resumo(b, hoje) if b.tipo == "carro" else None,
                  falta_plano=mt.falta_para_plano(b) if b.tipo == "carro" else [],
                  descricao_carro=mt.descricao_carro(b, hoje) if b.tipo == "carro" else "",
                  oficina=_movs_oficina(db, b, hoje) if b.tipo == "carro" else [],
                  via_verde=vv.do_carro(db, b, ini, hoje) if b.tipo == "carro" else [],
                  consumo=fuelio.consumo(db, b, ini, hoje) if b.tipo == "carro" else None,
                  consumo_total=fuelio.consumo(db, b, date.min, hoje) if b.tipo == "carro" else None)


@router.post("/{bid}/fuelio")
async def importar_fuelio(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual),
                          db: Session = Depends(get_db)):
    """CSV de sincronização do Fuelio (vehicle-N-sync.csv, ou o ZIP) deste carro."""
    form = await ler_form(request)
    b = _carro(db, u, bid)
    voltar = f"/patrimonio/{b.id}#fuelio"
    f = form.get("ficheiro")
    if f is None or not getattr(f, "filename", ""):
        flash(request, "Escolhe o ficheiro do Fuelio.", "erro")
        return RedirectResponse(voltar, status_code=303)
    try:
        conteudo = await f.read(5 * 1024 * 1024 + 1)
        if len(conteudo) > 5 * 1024 * 1024:
            raise ValueError("ficheiro demasiado grande")
        dados = fuelio.ler(f.filename, conteudo)
    except Exception as e:   # zip/csv corrompido ou de outra app
        flash(request, f"Não foi possível ler o ficheiro: {e}", "erro")
        return RedirectResponse(voltar, status_code=303)
    mat = norm_matricula(dados.veiculo.matricula)
    if mat and b.matricula and mat != norm_matricula(b.matricula):
        flash(request, f"O ficheiro é do carro com matrícula {dados.veiculo.matricula}, não de {b.matricula}.", "erro")
        return RedirectResponse(voltar, status_code=303)
    if mat and not b.matricula:
        b.matricula = dados.veiculo.matricula.upper()[:12]
    novos, atualizados, leituras = fuelio.importar(db, b, dados)
    ligados = fuelio.ligar(db, b.entidade_id)
    pt.atribuir(db, b.entidade_id)
    auditar(db, u, "fuelio", f"{b.nome}: {novos} novos, {atualizados} atualizados, {ligados} ligados")
    db.commit()
    flash(request, f"Fuelio: {novos} abastecimento{'s' if novos != 1 else ''} novo{'s' if novos != 1 else ''}"
                   + (f", {atualizados} atualizados" if atualizados else "")
                   + f", {leituras} leitura{'s' if leituras != 1 else ''} de km e {ligados} "
                   f"débito{'s' if ligados != 1 else ''} do banco ligado{'s' if ligados != 1 else ''}.")
    return RedirectResponse(voltar, status_code=303)


def _movs_oficina(db: Session, b: Bem, hoje: date) -> list[Movimento]:
    """Despesas do carro (não combustível) dos últimos 2 anos, para ligar a uma ida à oficina."""
    ligados = {m.movimento_id for m in b.manutencoes if m.movimento_id}
    movs = db.scalars(select(Movimento).where(Movimento.bem_id == b.id, Movimento.montante < 0,
                                              Movimento.data_mov >= rel.somar_meses(hoje, -24))
                      .order_by(Movimento.data_mov.desc())).all()
    return [m for m in movs if m.id not in ligados and not pt.e_combustivel(m)][:40]


def _carro(db: Session, u: Utilizador, bid: int) -> Bem:
    b, _ = _bem(db, u, bid, "editar")
    if b.tipo != "carro":
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return b


def _ler_data_km(form) -> tuple[date, int | None]:
    data = parse_data(str(form.get("data", ""))) if form.get("data") else date.today()
    t = str(form.get("km", "")).replace(" ", "").replace(".", "")
    if t and not t.isdigit():
        raise ValueError("Km inválidos.")
    if data > date.today():
        raise ValueError("A data não pode ser no futuro.")
    return data, int(t) if t else None


@router.post("/{bid}/km")
async def registar_km(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual),
                      db: Session = Depends(get_db)):
    form = await ler_form(request)
    b = _carro(db, u, bid)
    try:
        data, km = _ler_data_km(form)
        if km is None:
            raise ValueError("Indica os km.")
        mt.registar_km(db, b, data, km, nota=str(form.get("nota", "")).strip()[:200] or None)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "erro")
        return RedirectResponse(f"/patrimonio/{b.id}#km", status_code=303)
    db.commit()
    flash(request, f"Leitura de {km:,} km em {data.strftime('%d/%m/%Y')} registada.".replace(",", " "))
    return RedirectResponse(f"/patrimonio/{b.id}#manutencao", status_code=303)


@router.post("/{bid}/km/{rid}/apagar")
async def apagar_km(request: Request, bid: int, rid: int, u: Utilizador = Depends(utilizador_atual),
                    db: Session = Depends(get_db)):
    await ler_form(request)
    b = _carro(db, u, bid)
    r = db.get(RegistoKm, rid)
    if not r or r.bem_id != b.id:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    b.registos_km.remove(r)
    ultimo = mt.ultimo_registo(b)
    b.km = ultimo.km if ultimo else None
    db.commit()
    flash(request, "Leitura apagada.")
    return RedirectResponse(f"/patrimonio/{b.id}#km", status_code=303)


@router.post("/{bid}/plano")
async def gerar_plano(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual),
                      db: Session = Depends(get_db)):
    await ler_form(request)
    b = _carro(db, u, bid)
    try:
        n = mt.gerar_plano(db, b, date.today())
    except ia.ErroIA as e:
        db.rollback()
        flash(request, f"Não foi possível obter o plano: {e}", "erro")
        return RedirectResponse(f"/patrimonio/{b.id}#manutencao", status_code=303)
    auditar(db, u, "plano_manutencao", f"{b.nome}: {n} itens")
    db.commit()
    flash(request, f"Plano de manutenção atualizado: {n} operações.")
    return RedirectResponse(f"/patrimonio/{b.id}#manutencao", status_code=303)


@router.post("/{bid}/oficina")
async def registar_oficina(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual),
                           db: Session = Depends(get_db)):
    form = await ler_form(request)
    b = _carro(db, u, bid)
    nomes = {it.item for it in b.plano}
    itens = [str(i) for i in form.getlist("itens") if str(i) in nomes]
    outro = str(form.get("outro", "")).strip()[:120]
    if outro:
        itens.append(outro)
    try:
        data, km = _ler_data_km(form)
        custo = abs(parse_montante(str(form.get("custo")))) if form.get("custo") else None
    except ValueError as e:
        flash(request, str(e), "erro")
        return RedirectResponse(f"/patrimonio/{b.id}#manutencao", status_code=303)
    mid = str(form.get("movimento_id", ""))
    mov = db.get(Movimento, int(mid)) if mid.isdigit() else None
    if mov and mov.conta.entidade_id != b.entidade_id:
        mov = None
    if not itens:
        flash(request, "Escolhe o que foi feito (ou escreve em «Outro»).", "erro")
        return RedirectResponse(f"/patrimonio/{b.id}#manutencao", status_code=303)
    if custo is None and mov:
        custo = -mov.montante
    if km is not None:
        try:
            mt.registar_km(db, b, data, km, origem="oficina")
        except ValueError as e:
            db.rollback()
            flash(request, str(e), "erro")
            return RedirectResponse(f"/patrimonio/{b.id}#manutencao", status_code=303)
    b.manutencoes.append(Manutencao(bem_id=b.id, data=data, km=km, itens="\n".join(itens), custo=custo,
                                    movimento_id=mov.id if mov else None,
                                    nota=str(form.get("nota", "")).strip()[:300] or None))
    if mov and not mov.bem_id:
        mov.bem_id, mov.bem_manual = b.id, True
    auditar(db, u, "oficina", f"{b.nome}: {', '.join(itens)}")
    db.commit()
    flash(request, f"Ida à oficina registada ({len(itens)} operaç{'ões' if len(itens) != 1 else 'ão'}).")
    return RedirectResponse(f"/patrimonio/{b.id}#manutencao", status_code=303)


@router.post("/{bid}/oficina/{mid}/apagar")
async def apagar_oficina(request: Request, bid: int, mid: int, u: Utilizador = Depends(utilizador_atual),
                         db: Session = Depends(get_db)):
    await ler_form(request)
    b = _carro(db, u, bid)
    m = db.get(Manutencao, mid)
    if not m or m.bem_id != b.id:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    b.manutencoes.remove(m)
    db.commit()
    flash(request, "Registo de oficina apagado (a leitura de km fica).")
    return RedirectResponse(f"/patrimonio/{b.id}#manutencao", status_code=303)


def _area(txt: str, rotulo: str, erros: list) -> float | None:
    t = txt.strip().replace(" ", "")
    if not t:
        return None
    try:
        v = float(t.replace(".", "").replace(",", ".")) if "," in t else float(t)
        if not 0 < v < 10_000_000:
            raise ValueError
        return v
    except ValueError:
        erros.append(f"{rotulo} inválida (ex.: 110,95).")
        return None


@router.post("/{bid}/construcoes")
async def adicionar_construcao(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual),
                               db: Session = Depends(get_db)):
    form = await ler_form(request)
    b, _ = _bem(db, u, bid, "editar")
    voltar = f"/patrimonio/{b.id}#construcoes"
    if b.tipo != "rustico":
        raise HTTPException(status_code=404, detail="Não encontrado.")
    erros = []
    designacao = str(form.get("designacao", "")).strip()[:100]
    if not designacao:
        erros.append("Indica a designação (ex.: Casa 1, Armazém).")
    terreno = _area(str(form.get("area_terreno", "")), "Área de terreno", erros)
    bruta = _area(str(form.get("area_bruta", "")), "Área bruta", erros)
    if erros:
        flash(request, " ".join(erros), "erro")
        return RedirectResponse(voltar, status_code=303)
    tipo, estado = str(form.get("tipo", "")), str(form.get("estado", ""))
    cid = str(form.get("id", ""))
    c = next((x for x in b.construcoes if str(x.id) == cid), None) if cid.isdigit() else None
    dados = dict(designacao=designacao, tipo=tipo if tipo in TIPOS_CONSTRUCAO else None,
                 artigo=str(form.get("artigo", "")).strip()[:60] or None, area_terreno=terreno, area_bruta=bruta,
                 estado=estado if estado in ESTADOS_BEM else None)
    if c:
        for k, v in dados.items():
            setattr(c, k, v)
    else:
        b.construcoes.append(Construcao(bem_id=b.id, ordem=len(b.construcoes), **dados))
    auditar(db, u, "construcao", f"{b.nome}: {designacao}")
    db.commit()
    flash(request, f"Construção «{designacao}» {'guardada' if c else 'adicionada'}.")
    return RedirectResponse(voltar, status_code=303)


@router.post("/{bid}/construcoes/{cid}/apagar")
async def apagar_construcao(request: Request, bid: int, cid: int, u: Utilizador = Depends(utilizador_atual),
                            db: Session = Depends(get_db)):
    await ler_form(request)
    b, _ = _bem(db, u, bid, "editar")
    c = next((x for x in b.construcoes if x.id == cid), None)
    if not c:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    b.construcoes.remove(c)
    db.commit()
    flash(request, f"Construção «{c.designacao}» apagada.")
    return RedirectResponse(f"/patrimonio/{b.id}#construcoes", status_code=303)


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
              "omissao": "1" if b.omissao else "", "padroes": b.padroes or "", "notas": b.notas or "",
              "area_ha": ("%g" % b.area_ha).replace(".", ",") if b.area_ha else "", "ocupacao": b.lista_ocupacao,
              "agua": b.lista_agua, "tem_cortica": "1" if b.tem_cortica else ""})
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
    _km_da_ficha(db, b)
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
