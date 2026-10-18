"""Património: casas e carros — valor (histórico de avaliações, IA), dívida associada,
despesas por bem (casa principal vs. férias, cada carro) e atribuição das despesas."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import ia
from .. import manutencao as mt
from .. import fuelio, recheio
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
                  recheio_sem_casa=recheio.sem_casa(db, ent.id),
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


def _modulo_do_tipo(ent, tipo: str) -> None:
    modulo = "viaturas" if tipo == "carro" else "imoveis"
    if not ent.tem(modulo):
        raise HTTPException(status_code=400, detail=f"A entidade «{ent.nome}» não usa este tipo de bem "
                                                    "(Administração › Entidades › Editar).")


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
    _modulo_do_tipo(ent, tipo)
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
    _modulo_do_tipo(ent, tipo)
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
    ini = pt.inicio_periodo(db, b.entidade_id, hoje)
    valor, divida = pt.valor_atual(b), pt.divida(db, b, hoje)
    c = pt.custos(db, b, ini, hoje)
    return render(request, "patrimonio/ficha.html", visiveis=visiveis, entidade=ent, b=b, hoje=hoje, ini=ini,
                  valor=valor, divida=divida, c=c, proxima=pt.proxima_revisao(b),
                  pendente=pt.revisao_pendente(b, hoje), falta=pt.falta_para_ia(b), ia_disponivel=ia.disponivel(),
                  descricao_ia=pt.descricao_para_ia(b), estados=ESTADOS_BEM, combustiveis=COMBUSTIVEIS,
                  ocupacoes=OCUPACOES, aguas=AGUAS, acessos=ACESSOS, tipos_construcao=TIPOS_CONSTRUCAO,
                  construcoes=pt.construcoes_por_artigo(b) if b.tipo == "rustico" else None,
                  graficos={"bem": pt.serie_valor(b),
                            "despesas_bem": pt.serie_despesas(c)}, pode_editar=PAPEIS[papel] >= PAPEIS["editar"],
                  mr=mt.resumo(b, hoje) if b.tipo == "carro" else None,
                  falta_plano=mt.falta_para_plano(b) if b.tipo == "carro" else [],
                  descricao_carro=mt.descricao_carro(b, hoje) if b.tipo == "carro" else "",
                  via_verde=vv.do_carro(db, b, ini, hoje) if b.tipo == "carro" else [],
                  consumo=fuelio.consumo(db, b, ini, hoje) if b.tipo == "carro" else None,
                  rech=recheio.resumo_casa(db, b, hoje) if b.tipo == "casa" else None,
                  consumo_total=fuelio.consumo(db, b, date.min, hoje) if b.tipo == "carro" else None)


def importar_fuelio_bem(db: Session, b: Bem, nome: str, conteudo: bytes, u: Utilizador) -> tuple[str, bool]:
    """Importa o CSV/ZIP do Fuelio para o carro. Devolve (mensagem, erro)."""
    try:
        if len(conteudo) > 5 * 1024 * 1024:
            raise ValueError("ficheiro demasiado grande")
        dados = fuelio.ler(nome, conteudo)
    except Exception as e:   # zip/csv corrompido ou de outra app
        return f"Não foi possível ler o ficheiro: {e}", True
    mat = norm_matricula(dados.veiculo.matricula)
    if mat and b.matricula and mat != norm_matricula(b.matricula):
        return f"O ficheiro é do carro com matrícula {dados.veiculo.matricula}, não de {b.matricula}.", True
    if mat and not b.matricula:
        b.matricula = dados.veiculo.matricula.upper()[:12]
    novos, atualizados, leituras = fuelio.importar(db, b, dados)
    ligados = fuelio.ligar(db, b.entidade_id)
    pt.atribuir(db, b.entidade_id)
    auditar(db, u, "fuelio", f"{b.nome}: {novos} novos, {atualizados} atualizados, {ligados} ligados")
    db.commit()
    return (f"Fuelio ({b.nome}): {novos} abastecimento{'s' if novos != 1 else ''} novo{'s' if novos != 1 else ''}"
            + (f", {atualizados} atualizados" if atualizados else "")
            + f", {leituras} leitura{'s' if leituras != 1 else ''} de km e {ligados} "
            f"débito{'s' if ligados != 1 else ''} do banco ligado{'s' if ligados != 1 else ''}."), False


def _abastecimento(db: Session, u: Utilizador, bid: int, aid: int):
    from ..models import Abastecimento
    b = _carro(db, u, bid)
    a = db.get(Abastecimento, aid)
    if not a or a.bem_id != b.id:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return b, a


@router.post("/{bid}/abastecimento/{aid}")
async def corrigir_abastecimento(request: Request, bid: int, aid: int, u: Utilizador = Depends(utilizador_atual),
                                 db: Session = Depends(get_db)):
    """Corrige um abastecimento (ex.: valor mal escrito); «usar o valor do banco» copia o débito ligado."""
    form = await ler_form(request)
    b, a = _abastecimento(db, u, bid, aid)
    voltar = f"/patrimonio/{b.id}#fuelio"
    if form.get("usar_banco") and a.movimento:
        a.valor = -a.movimento.montante
        a.preco_litro = round(a.valor / 100 / a.litros, 3) if a.litros else None
        db.commit()
        flash(request, f"Valor corrigido para {fmt_montante(a.valor)} (o do banco).")
        return RedirectResponse(voltar, status_code=303)
    erros = []
    try:
        data = parse_data(str(form.get("data", ""))) or a.data
    except ValueError:
        data = a.data
        erros.append("Data inválida.")
    hora = str(form.get("hora", "")).strip()[:5] or None
    km = _inteiro(str(form.get("km", "")), "Km", erros, 0, 5_000_000)
    try:
        litros = float(str(form.get("litros", "")).replace(",", "."))
        if not 0 < litros < 500:
            raise ValueError
    except ValueError:
        litros = a.litros
        erros.append("Litros inválidos.")
    try:
        valor = abs(parse_montante(str(form.get("valor")))) if form.get("valor") else None
    except ValueError:
        valor = a.valor
        erros.append("Valor inválido.")
    if erros:
        flash(request, " ".join(erros), "erro")
        return RedirectResponse(voltar, status_code=303)
    mudou_ligacao = valor != a.valor or data != a.data
    # a leitura de km deste abastecimento acompanha a correção
    if km != a.km and a.km is not None:
        for r in b.registos_km:
            if r.data == a.data and r.km == a.km and r.origem in ("fuelio", "abastec"):
                r.km, r.data = km, data
                break
    a.data, a.hora, a.km, a.litros, a.valor = data, hora, km, litros, valor
    a.cheio = bool(form.get("cheio"))
    a.posto = str(form.get("posto", "")).strip()[:120] or None
    a.preco_litro = round(valor / 100 / litros, 3) if valor and litros else None
    if mudou_ligacao and a.movimento and (valor is None or abs(-a.movimento.montante - valor) > fuelio.TOLERANCIA
                                          or not (data - timedelta(days=fuelio.DIAS_ANTES) <= a.movimento.data_mov
                                                  <= data + timedelta(days=fuelio.DIAS_DEPOIS))):
        a.movimento_id = None          # deixou de bater com o débito: procura outro
    db.flush()
    ligados = fuelio.ligar(db, b.entidade_id)
    pt.atribuir(db, b.entidade_id)
    auditar(db, u, "abastecimento_corrigido", f"{b.nome} {a.data} {a.litros} l {a.valor}")
    db.commit()
    flash(request, "Abastecimento corrigido." + (" Ligado ao débito no banco." if ligados else ""))
    return RedirectResponse(voltar, status_code=303)


@router.post("/{bid}/abastecimento/{aid}/apagar")
async def apagar_abastecimento(request: Request, bid: int, aid: int, u: Utilizador = Depends(utilizador_atual),
                               db: Session = Depends(get_db)):
    await ler_form(request)
    b, a = _abastecimento(db, u, bid, aid)
    db.delete(a)
    db.flush()
    pt.atribuir(db, b.entidade_id)        # o débito que estava ligado volta à atribuição normal
    auditar(db, u, "abastecimento_apagado", f"{b.nome} {a.data} {a.litros} l")
    db.commit()
    flash(request, "Abastecimento apagado (a leitura de km fica).")
    return RedirectResponse(f"/patrimonio/{b.id}#fuelio", status_code=303)


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


def _ler_oficina(db: Session, b: Bem, form) -> dict:
    """Lê o formulário de uma ida à oficina (registar ou editar). ValueError com a mensagem se faltar algo."""
    nomes = {it.item for it in b.plano}
    itens = [str(i) for i in form.getlist("itens") if str(i) in nomes]
    for outro in str(form.get("outro", "")).replace(";", ",").split(","):
        outro = outro.strip()[:120]
        if outro and outro not in itens:
            itens.append(outro)
    data, km = _ler_data_km(form)
    custo = abs(parse_montante(str(form.get("custo")))) if form.get("custo") else None
    mid = str(form.get("movimento_id", ""))
    mov = db.get(Movimento, int(mid)) if mid.isdigit() else None
    if mov and mov.conta.entidade_id != b.entidade_id:
        mov = None
    if not itens:
        raise ValueError("escolhe o que foi feito (ou escreve em «Outro»).")
    if custo is None and mov:
        custo = -mov.montante
    return {"data": data, "km": km, "itens": itens, "custo": custo, "mov": mov,
            "nota": str(form.get("nota", "")).strip()[:300] or None}


def _msg_plano(b: Bem, itens: list[str]) -> str:
    if not b.plano:
        return ""
    msg = ""
    no_plano = [it.item for it in b.plano if any(mt.corresponde(x, it.item) for x in itens)]
    fora = [x for x in itens if not any(mt.corresponde(x, it.item) for it in b.plano)]
    if no_plano:
        msg += " Plano atualizado: " + ", ".join(no_plano) + "."
    if fora:
        msg += " Sem correspondência no plano (fica só nas idas à oficina): " + ", ".join(fora) + "."
    return msg


@router.post("/{bid}/oficina")
async def registar_oficina(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual),
                           db: Session = Depends(get_db)):
    form = await ler_form(request)
    b = _carro(db, u, bid)
    try:
        d = _ler_oficina(db, b, form)
        if d["km"] is not None:
            mt.registar_km(db, b, d["data"], d["km"], origem="oficina")
    except ValueError as e:
        db.rollback()
        flash(request, f"Ida à oficina não registada: {e}", "erro")
        return RedirectResponse(f"/patrimonio/{b.id}", status_code=303)
    mov = d["mov"]
    b.manutencoes.append(Manutencao(bem_id=b.id, data=d["data"], km=d["km"], itens="\n".join(d["itens"]), custo=d["custo"],
                                    movimento_id=mov.id if mov else None, nota=d["nota"]))
    if mov and not mov.bem_id:
        mov.bem_id, mov.bem_manual = b.id, True
    auditar(db, u, "oficina", f"{b.nome}: {', '.join(d['itens'])}")
    db.commit()
    n = len(d["itens"])
    flash(request, f"Ida à oficina de {d['data']:%d/%m/%Y} registada ({n} operaç{'ões' if n != 1 else 'ão'})."
                   + _msg_plano(b, d["itens"]))
    return RedirectResponse(f"/patrimonio/{b.id}#manutencao", status_code=303)


def _oficina(db: Session, b: Bem, mid: int) -> Manutencao:
    m = db.get(Manutencao, mid)
    if not m or m.bem_id != b.id:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return m


@router.get("/{bid}/oficina/movimentos")
def pesquisar_movimentos_oficina(bid: int, q: str = "", data: str = "", manutencao: str = "",
                                 u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Débitos da entidade do carro para associar a uma ida à oficina: por texto ou valor; sem pesquisa, os perto da
    data (os deste carro primeiro)."""
    from fastapi.responses import JSONResponse
    from sqlalchemy import func
    b, _ = _bem(db, u, bid, "editar")
    hoje = date.today()
    try:
        ref = parse_data(data) if data else hoje
    except ValueError:
        ref = hoje
    consulta = select(Movimento).join(Conta).where(Conta.entidade_id == b.entidade_id, Movimento.montante < 0,
                                                   ~Movimento.transferencia,
                                                   Movimento.data_mov >= hoje - timedelta(days=3 * 366))
    q = q.strip()
    valor = None
    if q:
        try:
            valor = abs(parse_montante(q))
        except ValueError:
            valor = None
    if valor:
        consulta = consulta.where(Movimento.montante == -valor)
    elif q:
        consulta = consulta.where(func.upper(Movimento.descricao_original).contains(q.upper()))
    else:
        consulta = consulta.where(Movimento.data_mov.between(ref - timedelta(days=45), ref + timedelta(days=45)))
    movs = db.scalars(consulta.order_by(Movimento.data_mov.desc()).limit(300)).all()
    if not q:
        movs = [m for m in movs if not pt.e_combustivel(m)]
    movs.sort(key=lambda m: (m.bem_id != b.id, abs((m.data_mov - ref).days)))
    excluir = int(manutencao) if manutencao.isdigit() else None
    ligados = {mid: bem for mid, bem in db.execute(select(Manutencao.movimento_id, Manutencao.bem_id)
                                                  .where(Manutencao.movimento_id.in_([m.id for m in movs[:25]]),
                                                         Manutencao.id != (excluir or 0)))}
    return JSONResponse([{"id": m.id, "data": f"{m.data_mov:%d/%m/%Y}", "descricao": m.descricao_original,
                          "conta": m.conta.nome, "valor": fmt_montante(m.montante), "custo": montante_para_input(-m.montante),
                          "deste_carro": m.bem_id == b.id, "ligado": m.id in ligados} for m in movs[:25]])


@router.get("/{bid}/oficina/{mid}/editar")
def editar_oficina(request: Request, bid: int, mid: int, u: Utilizador = Depends(utilizador_atual),
                   db: Session = Depends(get_db)):
    b = _carro(db, u, bid)
    m = _oficina(db, b, mid)
    visiveis, ent, _ = entidade_escolhida(request, db, u, b.entidade_id)
    return render(request, "patrimonio/oficina_editar.html", b=b, m=m, hoje=date.today(), entidade=ent,
                  montante_para_input=montante_para_input)


@router.post("/{bid}/oficina/{mid}/editar")
async def guardar_oficina(request: Request, bid: int, mid: int, u: Utilizador = Depends(utilizador_atual),
                          db: Session = Depends(get_db)):
    form = await ler_form(request)
    b = _carro(db, u, bid)
    m = _oficina(db, b, mid)
    try:
        d = _ler_oficina(db, b, form)
        if (d["data"], d["km"]) != (m.data, m.km):
            # a leitura de km desta ida à oficina acompanha a alteração
            antiga = next((r for r in b.registos_km if r.origem == "oficina" and r.data == m.data and r.km == m.km), None)
            if antiga:
                b.registos_km.remove(antiga)
                db.flush()
            if d["km"] is not None:
                mt.registar_km(db, b, d["data"], d["km"], origem="oficina")
            ultimo = mt.ultimo_registo(b)
            b.km = ultimo.km if ultimo else b.km
    except ValueError as e:
        db.rollback()
        flash(request, f"Alteração não guardada: {e}", "erro")
        return RedirectResponse(f"/patrimonio/{b.id}/oficina/{m.id}/editar", status_code=303)
    mov = d["mov"]
    m.data, m.km, m.itens, m.custo, m.nota = d["data"], d["km"], "\n".join(d["itens"]), d["custo"], d["nota"]
    m.movimento_id = mov.id if mov else None
    if mov and not mov.bem_id:
        mov.bem_id, mov.bem_manual = b.id, True
    auditar(db, u, "oficina_editada", f"{b.nome}: {m.data} {', '.join(d['itens'])}")
    db.commit()
    flash(request, "Ida à oficina guardada." + (" Movimento associado." if mov else "") + _msg_plano(b, d["itens"]))
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


# ------------------------------------------------------------------ recheio das casas
def _item(db: Session, u: Utilizador, iid: int, minimo: str = "ver"):
    from ..models import ItemRecheio
    it = db.get(ItemRecheio, iid)
    if not it:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return it, exigir_papel(db, u, it.entidade_id, minimo)


@router.get("/recheio/{iid}")
def ficha_item(request: Request, iid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    from ..models import ESTADOS_PEDIDO, ESTADOS_RECHEIO, TIPOS_RECHEIO
    it, papel = _item(db, u, iid)
    visiveis, ent, _ = entidade_escolhida(request, db, u, it.entidade_id)
    casas = db.scalars(select(Bem).where(Bem.entidade_id == it.entidade_id, Bem.tipo == "casa").order_by(Bem.nome)).all()
    return render(request, "patrimonio/item_recheio.html", visiveis=visiveis, entidade=ent, it=it, casas=casas,
                  tipos=TIPOS_RECHEIO, estados=ESTADOS_RECHEIO, estados_pedido=ESTADOS_PEDIDO, hoje=date.today(),
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


def _ler_item(form, db: Session, entidade_id: int, erros: list) -> dict:
    from ..models import ESTADOS_RECHEIO, TIPOS_RECHEIO
    v = {k: str(form.get(k, "")).strip() for k in ("nome", "tipo", "marca", "modelo", "n_serie", "atributos", "data_compra",
                                                     "valor", "garantia_meses", "loja", "contacto_telefone",
                                                     "contacto_email", "contacto_site", "estado", "notas", "bem_id")}
    if not v["nome"]:
        erros.append("Indica o nome do item.")
    try:
        data = parse_data(v["data_compra"]) if v["data_compra"] else None
    except ValueError:
        data = None
        erros.append("Data da compra inválida.")
    try:
        valor = abs(parse_montante(v["valor"])) if v["valor"] else None
    except ValueError:
        valor = None
        erros.append("Valor inválido.")
    garantia = _inteiro(v["garantia_meses"], "Garantia (meses)", erros, 0, 240)
    casa = db.get(Bem, int(v["bem_id"])) if v["bem_id"].isdigit() else None
    return dict(nome=v["nome"][:120], tipo=v["tipo"] if v["tipo"] in TIPOS_RECHEIO else None, marca=v["marca"][:60] or None,
                modelo=v["modelo"][:80] or None, n_serie=v["n_serie"][:60] or None, atributos=v["atributos"] or None,
                data_compra=data, valor=valor, garantia_meses=garantia if garantia is not None else 0,
                loja=v["loja"][:120] or None, contacto_telefone=v["contacto_telefone"][:40] or None,
                contacto_email=v["contacto_email"][:120] or None, contacto_site=v["contacto_site"][:200] or None,
                estado=v["estado"] if v["estado"] in ESTADOS_RECHEIO else "ativo", notas=v["notas"] or None,
                bem_id=casa.id if casa and casa.entidade_id == entidade_id and casa.tipo == "casa" else None)


@router.post("/recheio/{iid}")
async def guardar_item(request: Request, iid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    it, _ = _item(db, u, iid, "editar")
    erros = []
    dados = _ler_item(form, db, it.entidade_id, erros)
    if erros:
        flash(request, " ".join(erros), "erro")
        return RedirectResponse(f"/patrimonio/recheio/{it.id}", status_code=303)
    for k, v in dados.items():
        setattr(it, k, v)
    auditar(db, u, "recheio_item", f"{it.nome} ({it.estado})")
    db.commit()
    flash(request, "Item guardado." if it.estado != "ignorado" else "Marcado como «não é recheio» (sai do inventário).")
    return RedirectResponse(f"/patrimonio/{it.bem_id}#recheio" if it.bem_id and it.estado == "ignorado"
                            else f"/patrimonio/recheio/{it.id}", status_code=303)


@router.post("/{bid}/recheio/novo")
async def novo_item(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Item do recheio sem compra no extrato (ex.: comprado antes, oferecido)."""
    from ..models import ItemRecheio
    form = await ler_form(request)
    b, _ = _bem(db, u, bid, "editar")
    nome = str(form.get("nome", "")).strip()[:120]
    if not nome:
        flash(request, "Indica o nome do item.", "erro")
        return RedirectResponse(f"/patrimonio/{b.id}#recheio", status_code=303)
    from .. import recheio
    it = ItemRecheio(entidade_id=b.entidade_id, bem_id=b.id, nome=nome, garantia_meses=recheio.garantia_omissao(db))
    db.add(it)
    db.commit()
    return RedirectResponse(f"/patrimonio/recheio/{it.id}", status_code=303)


@router.post("/recheio/{iid}/apagar")
async def apagar_item(request: Request, iid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    it, _ = _item(db, u, iid, "editar")
    voltar = f"/patrimonio/{it.bem_id}#recheio" if it.bem_id else f"/patrimonio?entidade={it.entidade_id}"
    if it.movimento_id:
        it.estado = "ignorado"          # com compra no extrato: fica marcado, para não voltar a ser criado
        msg = "Marcado como «não é recheio»."
    else:
        db.delete(it)
        msg = "Item apagado."
    db.commit()
    flash(request, msg)
    return RedirectResponse(voltar, status_code=303)


@router.post("/recheio/{iid}/pedidos")
async def pedido_garantia(request: Request, iid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Novo pedido de garantia (sem id) ou atualização do estado de um existente (com id)."""
    from ..models import ESTADOS_PEDIDO, PedidoGarantia
    form = await ler_form(request)
    it, _ = _item(db, u, iid, "editar")
    pid = str(form.get("id", ""))
    estado = str(form.get("estado", "aberto"))
    estado = estado if estado in ESTADOS_PEDIDO else "aberto"
    try:
        data = parse_data(str(form.get("data", ""))) if form.get("data") else date.today()
    except ValueError:
        data = date.today()
    p = next((x for x in it.pedidos if str(x.id) == pid), None) if pid.isdigit() else None
    if p:
        p.estado = estado
        p.resolucao = str(form.get("resolucao", "")).strip() or p.resolucao
        p.referencia = str(form.get("referencia", "")).strip()[:60] or p.referencia
        if estado in ("resolvido", "recusado") and not p.data_fecho:
            p.data_fecho = date.today()
        if it.estado == "avariado" and estado == "resolvido":
            it.estado = "ativo"
        msg = "Pedido de garantia atualizado."
    else:
        problema = str(form.get("problema", "")).strip()
        if not problema:
            flash(request, "Descreve o problema.", "erro")
            return RedirectResponse(f"/patrimonio/recheio/{it.id}#pedidos", status_code=303)
        it.pedidos.append(PedidoGarantia(item_id=it.id, data=data, problema=problema, estado="aberto",
                                         referencia=str(form.get("referencia", "")).strip()[:60] or None))
        if it.estado == "ativo":
            it.estado = "avariado"
        msg = "Pedido de garantia registado."
    auditar(db, u, "pedido_garantia", f"{it.nome}: {estado}")
    db.commit()
    flash(request, msg)
    return RedirectResponse(f"/patrimonio/recheio/{it.id}#pedidos", status_code=303)


@router.post("/{bid}/capital-recheio")
async def capital_recheio(request: Request, bid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    b, _ = _bem(db, u, bid, "editar")
    txt = str(form.get("capital", "")).strip()
    try:
        b.capital_recheio = abs(parse_montante(txt)) if txt else None
    except ValueError as e:
        flash(request, f"Capital inválido: {e}", "erro")
        return RedirectResponse(f"/patrimonio/{b.id}#recheio", status_code=303)
    db.commit()
    flash(request, "Capital seguro do recheio guardado.")
    return RedirectResponse(f"/patrimonio/{b.id}#recheio", status_code=303)


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
