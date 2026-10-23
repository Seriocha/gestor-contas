"""Administração › Paperless: correspondentes, fluxos de trabalho, email, manutenção dos documentos e o motor de
classificação (regras, revisão, reclassificação em massa, auditoria — app/motor_docs.py).

Os circuitos (criar o que um tipo de documento precisa) continuam em /admin/paperless (routes/admin.py). Todas estas
páginas usam o token do utilizador escolhido no topo (guardado na sessão); o Paperless decide o que esse token pode
ver e alterar.
"""
import json as _json
import os as _os
import re as _re
from datetime import date
from urllib.parse import urlencode as _urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ia as _ia
from .. import motor_docs as md
from .. import paperless
from .. import paperless_gestao as pg
from .. import paperless_manutencao as pm
from .. import preferencias
from ..deps import auditar, exigir_admin, flash, get_db, render
from ..models import AlteracaoDocumento, ClassificacaoDocumento, RegraDocumento, RegraDocumentoVersao, Utilizador
from ..seguranca import ler_form

router = APIRouter(prefix="/admin/paperless")

FERRAMENTAS = {"valores": "Extrair valor", "datas": "Corrigir datas", "titulos": "Atualizar títulos",
               "duplicados": "Duplicados", "diagnostico": "Diagnóstico"}


def _token(request: Request, db: Session, admin: Utilizador) -> tuple[dict, str | None]:
    from .admin import _utilizadores_paperless
    utilizadores = _utilizadores_paperless(request, db)
    uid = request.session.get("paperless_uid") or admin.id
    u = db.get(Utilizador, uid)
    token = paperless.token_de(u, request.app.state.defs.secret_key) if u else None
    ctx = {"utilizadores": utilizadores, "uid": uid, "token_ok": bool(token), "paperless_cfg": paperless.configurado(),
           "paperless_link": paperless.url_browser(), "algoritmos": pg.ALGORITMOS, "erros": []}
    return ctx, token


def _int(v, omissao=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return omissao


def _data(v) -> date | None:
    try:
        return date.fromisoformat(str(v or "").strip())
    except ValueError:
        return None


def _lista(v) -> list[str]:
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def _carregar(ctx: dict, chave: str, funcao):
    """Lê do Paperless para a página; um erro de ligação vira aviso em vez de página de erro."""
    try:
        ctx[chave] = funcao()
    except paperless.ErroPaperless as e:
        ctx[chave] = None
        ctx["erros"].append(str(e))


@router.get("/token")
def escolher_token(request: Request, uid: int, volta: str = "/admin/paperless/correspondentes",
                   admin: Utilizador = Depends(exigir_admin)):
    request.session["paperless_uid"] = uid
    return RedirectResponse(volta if volta.startswith("/admin/paperless") else "/admin/paperless/correspondentes", status_code=303)


# ------------------------------------------------------------------ correspondentes
@router.get("/correspondentes")
def correspondentes(request: Request, q: str = "", admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    ctx, token = _token(request, db, admin)
    if token:
        _carregar(ctx, "lista", lambda: pg.correspondentes(token))
    lista = ctx.get("lista") or []
    ctx["todos"] = lista
    ctx["lista"] = [c for c in lista if q.lower() in (c.get("name") or "").lower()] if q else lista
    return render(request, "admin/paperless_correspondentes.html", q=q, **ctx)


@router.post("/correspondentes")
async def correspondentes_acao(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    _, token = _token(request, db, admin)
    acao, volta = str(form.get("acao", "")), "/admin/paperless/correspondentes"
    try:
        if not token:
            raise paperless.ErroPaperless("O utilizador escolhido não tem token do Paperless.")
        if acao == "guardar":
            nome = str(form.get("nome", "")).strip()[:128]
            if not nome:
                raise paperless.ErroPaperless("Indica o nome do correspondente.")
            cid = _int(form.get("id"))
            pg.guardar_correspondente(token, cid, nome, _int(form.get("algoritmo"), 0), str(form.get("match", ""))[:256],
                                      bool(form.get("insensivel")))
            auditar(db, admin, "paperless_correspondente", f"{'editado' if cid else 'criado'}: {nome}")
            flash(request, f"Correspondente «{nome}» {'guardado' if cid else 'criado'}.")
        elif acao == "apagar":
            cid = _int(form.get("id"))
            pg.apagar("correspondents", cid, token)
            auditar(db, admin, "paperless_correspondente", f"apagado: #{cid} {form.get('nome', '')}")
            flash(request, f"Correspondente «{form.get('nome', '')}» apagado (os documentos ficam sem correspondente).")
        elif acao == "fundir":
            origem, destino = _int(form.get("origem")), _int(form.get("destino"))
            if not origem or not destino:
                raise paperless.ErroPaperless("Escolhe o correspondente a juntar e o que fica.")
            n = pg.fundir_correspondentes(token, origem, destino)
            auditar(db, admin, "paperless_correspondente", f"fundido #{origem} → #{destino} ({n} documentos)")
            flash(request, f"Correspondentes fundidos: {n} documento{'s' if n != 1 else ''} mudado{'s' if n != 1 else ''}.")
        db.commit()
    except paperless.ErroPaperless as e:
        flash(request, str(e), "erro")
    return RedirectResponse(volta, status_code=303)


# ------------------------------------------------------------------ fluxos
@router.get("/fluxos")
def fluxos(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    ctx, token = _token(request, db, admin)
    if token:
        _carregar(ctx, "fluxos", lambda: pg.fluxos(token))
    return render(request, "admin/paperless_fluxos.html", gatilhos=pg.GATILHOS, **ctx)


@router.post("/fluxos")
async def fluxos_acao(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    _, token = _token(request, db, admin)
    acao, fid, nome = str(form.get("acao", "")), _int(form.get("id")), str(form.get("nome", ""))
    try:
        if not token:
            raise paperless.ErroPaperless("O utilizador escolhido não tem token do Paperless.")
        if acao in ("ativar", "desativar"):
            pg.alterar_fluxo(token, fid, ativo=acao == "ativar")
            flash(request, f"Fluxo «{nome}» {'ativado' if acao == 'ativar' else 'desativado'}.")
        elif acao == "guardar":
            novo_nome = str(form.get("novo_nome", "")).strip()[:256] or None
            pg.alterar_fluxo(token, fid, ordem=_int(form.get("ordem")), nome=novo_nome)
            flash(request, f"Fluxo «{novo_nome or nome}» guardado.")
        elif acao == "gatilho":
            pg.alterar_gatilho(token, _int(form.get("gatilho")), _int(form.get("algoritmo"), 0),
                               str(form.get("match", ""))[:256], bool(form.get("insensivel")))
            flash(request, f"Gatilho do fluxo «{nome}» guardado.")
        elif acao == "apagar":
            pg.apagar("workflows", fid, token)
            flash(request, f"Fluxo «{nome}» apagado.")
        else:
            return RedirectResponse("/admin/paperless/fluxos", status_code=303)
        auditar(db, admin, "paperless_fluxo", f"{acao}: #{fid} {nome}")
        db.commit()
    except paperless.ErroPaperless as e:
        flash(request, str(e), "erro")
    return RedirectResponse(f"/admin/paperless/fluxos#fluxo-{fid}", status_code=303)


# ------------------------------------------------------------------ email
@router.get("/email")
def email(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    ctx, token = _token(request, db, admin)
    if token:
        _carregar(ctx, "rev", lambda: pg.rever_email(token))
    return render(request, "admin/paperless_email.html", **ctx)


@router.post("/email")
async def email_acao(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    _, token = _token(request, db, admin)
    acao, rid, nome = str(form.get("acao", "")), _int(form.get("id")), str(form.get("nome", ""))
    try:
        if not token:
            raise paperless.ErroPaperless("O utilizador escolhido não tem token do Paperless.")
        if acao == "corrigir":
            pg.corrigir_regra(token, rid)
            flash(request, f"Regra «{nome}» corrigida: o parâmetro vazio passou a nulo.")
        elif acao == "corrigir_todas":
            n = 0
            for r in pg.rever_email(token)["regras"]:
                if r["corrigivel"]:
                    pg.corrigir_regra(token, r["id"])
                    n += 1
            flash(request, f"{n} regra{'s' if n != 1 else ''} corrigida{'s' if n != 1 else ''}.")
        elif acao in ("ativar", "desativar"):
            pg.alterar("mail_rules", rid, {"enabled": acao == "ativar"}, token)
            flash(request, f"Regra «{nome}» {'ativada' if acao == 'ativar' else 'desativada'}.")
        else:
            return RedirectResponse("/admin/paperless/email", status_code=303)
        auditar(db, admin, "paperless_email", f"{acao}: {nome or rid}")
        db.commit()
    except paperless.ErroPaperless as e:
        flash(request, str(e), "erro")
    return RedirectResponse("/admin/paperless/email", status_code=303)


# ------------------------------------------------------------------ manutenção dos documentos
VALORES_OMISSAO = {"tipos": "Fatura", "tipos_datas": "", "desde": "", "ano_min": "2015", "modelo": pm.MODELO_TITULO,
                   "limiar": "0.90", "limite": str(pm.LIMITE), "doc": "", "correspondente": ""}


def _ctx_manutencao(request, db, admin, ferramenta: str, valores: dict):
    ctx, token = _token(request, db, admin)
    ctx.update(ferramentas=FERRAMENTAS, ferramenta=ferramenta if ferramenta in FERRAMENTAS else "valores",
               v={**VALORES_OMISSAO, **valores}, resultado=None, grupos=None, n_docs=0, diag=None)
    if token and ctx["ferramenta"] in ("titulos", "duplicados"):
        _carregar(ctx, "correspondentes", lambda: sorted(pg.listar("correspondents", token, ordering="name"),
                                                         key=lambda c: (c.get("name") or "").lower()))
    return ctx, token


@router.get("/manutencao")
def manutencao(request: Request, ferramenta: str = "valores", admin: Utilizador = Depends(exigir_admin),
               db: Session = Depends(get_db)):
    ctx, _ = _ctx_manutencao(request, db, admin, ferramenta, {})
    return render(request, "admin/paperless_manutencao.html", **ctx)


@router.post("/manutencao")
async def manutencao_acao(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    ferramenta, acao = str(form.get("ferramenta", "valores")), str(form.get("acao", "propor"))
    valores = {k: str(form.get(k, "")).strip()[:200] for k in VALORES_OMISSAO}
    valores.update(reprocessar=bool(form.get("reprocessar")), todos=bool(form.get("todos")))
    ctx, token = _ctx_manutencao(request, db, admin, ferramenta, valores)
    ferramenta = ctx["ferramenta"]
    if not token:
        ctx["erros"].append("O utilizador escolhido não tem token do Paperless.")
        return render(request, "admin/paperless_manutencao.html", status_code=400, **ctx)
    limite = min(max(_int(valores["limite"], pm.LIMITE), 1), 5000)
    try:
        if acao == "aplicar":
            escolhidos = {int(i): str(form.get(f"v_{i}", "")) for i in form.getlist("sel") if str(i).isdigit()}
            escolhidos = {k: v for k, v in escolhidos.items() if v}
            if ferramenta == "duplicados":
                ok, erros = pm.marcar_duplicados(token, list(escolhidos))
            else:
                aplicar = {"valores": pm.aplicar_valores, "datas": pm.aplicar_datas, "titulos": pm.aplicar_titulos}[ferramenta]
                ok, erros = aplicar(token, escolhidos)
            auditar(db, admin, "paperless_manutencao", f"{ferramenta}: {ok} documentos alterados, {len(erros)} erros")
            db.commit()
            flash(request, f"{FERRAMENTAS[ferramenta]}: {ok} documento{'s' if ok != 1 else ''} alterado{'s' if ok != 1 else ''}.")
            for e in erros[:10]:
                flash(request, e, "erro")
            return RedirectResponse(f"/admin/paperless/manutencao?ferramenta={ferramenta}", status_code=303)
        desde = _data(valores["desde"])
        if ferramenta == "valores":
            ctx["resultado"] = pm.propor_valores(token, _lista(valores["tipos"]) or ["Fatura"], desde, valores["reprocessar"], limite)
        elif ferramenta == "datas":
            ctx["resultado"] = pm.propor_datas(token, _lista(valores["tipos_datas"]), desde, _int(valores["ano_min"], 2015),
                                               valores["todos"], limite)
        elif ferramenta == "titulos":
            ctx["resultado"] = pm.propor_titulos(token, valores["modelo"] or pm.MODELO_TITULO, _lista(valores["tipos_datas"]),
                                                 _int(valores["correspondente"]), desde, limite)
        elif ferramenta == "duplicados":
            cid = _int(valores["correspondente"])
            if not cid:
                ctx["erros"].append("Escolhe o correspondente.")
            else:
                try:
                    limiar = min(max(float(valores["limiar"].replace(",", ".")), 0.5), 1.0)
                except ValueError:
                    limiar = 0.90
                ctx["grupos"], ctx["n_docs"] = pm.duplicados(token, cid, limiar)
        elif ferramenta == "diagnostico":
            pid = _int(valores["doc"].rstrip("/").split("/documents/")[-1].split("/")[0])
            if not pid:
                ctx["erros"].append("Indica o n.º do documento (o número no endereço …/documents/12/details).")
            else:
                ctx["diag"] = pm.diagnostico(token, pid)
    except paperless.ErroPaperless as e:
        ctx["erros"].append(str(e))
    return render(request, "admin/paperless_manutencao.html", status_code=400 if ctx["erros"] else 200, **ctx)


# ------------------------------------------------------------------ motor de classificação: regras
CAMPOS_REGRA = ("nome", "algoritmo", "texto", "so_tipo", "correspondente", "tipo", "etiquetas", "extrair", "confianca", "nota")


def _contagem_estados(db) -> dict:
    return dict(db.execute(select(ClassificacaoDocumento.estado, func.count()).group_by(ClassificacaoDocumento.estado)).all())


def _ctx_motor(request, db, admin):
    ctx, token = _token(request, db, admin)
    estados = _contagem_estados(db)
    ctx.update(algoritmos_motor=md.ALGORITMOS, atributos=md.ATRIBUTOS, confiancas=md.CONFIANCAS, estados=md.ESTADOS,
               contagem_estados=estados, n_rever=estados.get("revisao", 0))
    return ctx, token


@router.get("/regras")
def regras(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    ctx, _ = _ctx_motor(request, db, admin)
    lista = db.scalars(select(RegraDocumento).order_by(RegraDocumento.ordem, RegraDocumento.id)).all()
    contagem = {rid: n for rid, n in db.execute(select(ClassificacaoDocumento.regra_id, func.count())
                                                .group_by(ClassificacaoDocumento.regra_id)).all()}
    return render(request, "admin/paperless_regras.html", regras=lista, contagem=contagem, prefs=request.app.state.prefs,
                  ciclo_possivel=bool((_os.environ.get("GESTOR_PAPERLESS_TOKEN") or "").strip()),
                  publicavel=md.publicavel, ia_ok=_ia.disponivel(), **ctx)


@router.post("/regras/config")
async def regras_config(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    for chave in ("motor_ativo", "motor_ia", "motor_ligar"):
        preferencias.gravar(db, chave, "1" if form.get(chave) else "0")
    preferencias.gravar(db, "motor_intervalo_minutos", str(min(max(_int(form.get("intervalo"), 30), 5), 1440)))
    d = _data(form.get("desde"))
    preferencias.gravar(db, "motor_desde", d.isoformat() if d else "")
    auditar(db, admin, "paperless_motor", "configuração")
    db.commit()
    request.app.state.prefs = preferencias.carregar(db)
    flash(request, "Configuração do motor guardada.")
    return RedirectResponse("/admin/paperless/regras", status_code=303)


@router.post("/regras/correr")
async def regras_correr(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    await ler_form(request)
    _, token = _token(request, db, admin)
    if not token:
        flash(request, "O utilizador escolhido não tem token do Paperless.", "erro")
        return RedirectResponse("/admin/paperless/regras", status_code=303)
    try:
        r = md.processar(db, token, request.app.state.prefs, limite=50, u=admin)
        auditar(db, admin, "paperless_motor", f"correr: {r.lidos} lidos, {r.aplicados} aplicados, {r.revisao} rever")
        db.commit()
        flash(request, f"Motor: {r.lidos} documentos novos lidos · {r.aplicados} aplicados · {r.revisao} para rever · "
                       f"{r.sem_regra} sem regra · {r.ligados} ligados a movimentos.")
        for e in r.erros[:5]:
            flash(request, e, "erro")
    except paperless.ErroPaperless as e:
        flash(request, str(e), "erro")
    return RedirectResponse("/admin/paperless/regras", status_code=303)


def _form_regra(request, db, admin, r, valores: dict, status_code: int = 200, **extra):
    ctx, _ = _ctx_motor(request, db, admin)
    versoes = db.scalars(select(RegraDocumentoVersao).where(RegraDocumentoVersao.regra_id == r.id)
                         .order_by(RegraDocumentoVersao.versao.desc())).all() if r is not None and r.id else []
    ultimos = db.scalars(select(ClassificacaoDocumento).where(ClassificacaoDocumento.regra_id == r.id)
                         .order_by(ClassificacaoDocumento.processado_em.desc()).limit(15)).all() if r is not None and r.id else []
    ctx.update(teste=None, teste_doc=None, erros_form=[])
    ctx.update(extra)
    return render(request, "admin/paperless_regra.html", status_code=status_code, regra=r, v=valores, versoes=versoes,
                  ultimos=ultimos, motivo_publicar=md.publicavel(r) if r is not None and r.id else None, **ctx)


def _valores_regra(r, origem: dict | None = None) -> dict:
    base = {"nome": "", "algoritmo": "exato", "texto": "", "so_tipo": "", "correspondente": "", "tipo": "", "etiquetas": "",
            "extrair": "valor,data_emissao", "confianca": "alta", "nota": "", "ordem": "100", "ativa": True,
            "so_sem_correspondente": False, "origem": "manual"}
    if r is not None:
        base.update({k: getattr(r, k) or "" for k in CAMPOS_REGRA})
        base.update(ordem=str(r.ordem), ativa=r.ativa, so_sem_correspondente=r.so_sem_correspondente, origem=r.origem)
    if origem:
        base.update({k: v for k, v in origem.items() if k in base and v is not None})
    return base


@router.get("/regras/nova")
def regra_nova(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    pre = {k: request.query_params.get(k) for k in ("nome", "texto", "algoritmo", "correspondente", "tipo", "extrair")
           if request.query_params.get(k)}
    if pre:
        pre.update(confianca="media", origem="sugestao")     # regra sugerida: começa por mandar para revisão
    return _form_regra(request, db, admin, None, _valores_regra(None, pre))


@router.get("/regras/{rid}")
def regra_ver(request: Request, rid: int, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    r = db.get(RegraDocumento, rid)
    if not r:
        return RedirectResponse("/admin/paperless/regras", status_code=303)
    return _form_regra(request, db, admin, r, _valores_regra(r))


@router.post("/regras/guardar")
async def regra_guardar(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    rid = _int(form.get("id"))
    r = db.get(RegraDocumento, rid) if rid else None
    v = {k: str(form.get(k, "")).strip() for k in CAMPOS_REGRA}
    v["extrair"] = ",".join(x for x in form.getlist("extrair_k") if x in md.ATRIBUTOS)
    v.update(ordem=str(_int(form.get("ordem"), 100)), ativa=bool(form.get("ativa")),
             so_sem_correspondente=bool(form.get("so_sem_correspondente")), origem=str(form.get("origem") or "manual")[:10])
    erros = []
    if not v["nome"]:
        erros.append("Indica o nome da regra.")
    elif db.scalar(select(RegraDocumento).where(func.lower(RegraDocumento.nome) == v["nome"].lower(),
                                                RegraDocumento.id != (rid or 0))):
        erros.append("Já existe uma regra com esse nome.")
    if not v["texto"]:
        erros.append("Indica o texto que reconhece o documento.")
    if v["algoritmo"] not in md.ALGORITMOS:
        v["algoritmo"] = "todas"
    if v["algoritmo"] == "regex":
        try:
            _re.compile(v["texto"])
        except _re.error as e:
            erros.append(f"Expressão regular inválida: {e}.")
    if v["confianca"] not in md.CONFIANCAS:
        v["confianca"] = "media"
    if not (v["correspondente"] or v["tipo"] or v["etiquetas"] or v["extrair"]):
        erros.append("A regra tem de fazer alguma coisa: correspondente, tipo, etiquetas ou atributos.")
    if erros:
        return _form_regra(request, db, admin, r, v, 400, erros_form=erros)
    dados = {k: (v[k] or None) if k in ("so_tipo", "correspondente", "tipo", "nota") else v[k] for k in CAMPOS_REGRA}
    dados.update(ordem=int(v["ordem"]), ativa=v["ativa"], so_sem_correspondente=v["so_sem_correspondente"])
    r = md.guardar_regra(db, r or RegraDocumento(origem=v["origem"]), dados, admin, str(form.get("nota_versao", "")).strip())
    if r.paperless_fluxo_id:                         # publicada: manter o fluxo nativo igual à regra
        _, token = _token(request, db, admin)
        try:
            if token and not md.publicavel(r):
                md.publicar(r, token)
            elif token:
                md.despublicar(r, token)
                flash(request, "A regra deixou de poder ser um fluxo nativo: o fluxo no Paperless foi apagado.", "erro")
        except paperless.ErroPaperless as e:
            flash(request, f"Regra guardada, mas o fluxo no Paperless não foi atualizado: {e}", "erro")
    auditar(db, admin, "paperless_regra", f"{r.nome} v{r.versao}")
    db.commit()
    flash(request, f"Regra «{r.nome}» guardada (versão {r.versao}).")
    return RedirectResponse(f"/admin/paperless/regras/{r.id}", status_code=303)


@router.post("/regras/{rid}/acao")
async def regra_acao(request: Request, rid: int, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    r = db.get(RegraDocumento, rid)
    if not r:
        return RedirectResponse("/admin/paperless/regras", status_code=303)
    acao = str(form.get("acao", ""))
    _, token = _token(request, db, admin)
    try:
        if acao == "testar":
            pid = _int(str(form.get("doc", "")).rstrip("/").split("/documents/")[-1].split("/")[0])
            if not pid or not token:
                raise paperless.ErroPaperless("Indica o n.º do documento (e escolhe um utilizador com token).")
            d = paperless._pedido(f"/api/documents/{pid}/", {"fields": md.CAMPOS_DOC}, token=token)
            ativa = r.ativa
            r.ativa = True                       # testar mesmo desligada (não é gravado)
            p = md.classificar(d, [r], md.Contexto(token), substituir=bool(form.get("substituir")))
            r.ativa = ativa
            return _form_regra(request, db, admin, r, _valores_regra(r), teste=p, teste_doc=pid)
        if acao == "apagar":
            if r.paperless_fluxo_id and token:
                md.despublicar(r, token)
            nome = r.nome
            db.delete(r)
            auditar(db, admin, "paperless_regra", f"apagada: {nome}")
            db.commit()
            flash(request, f"Regra «{nome}» apagada.")
            return RedirectResponse("/admin/paperless/regras", status_code=303)
        if acao == "publicar":
            if not token:
                raise paperless.ErroPaperless("O utilizador escolhido não tem token do Paperless.")
            fid = md.publicar(r, token)
            flash(request, f"Publicada no Paperless como o fluxo «Gestor · {r.nome}» (#{fid}).")
        elif acao == "despublicar":
            if token:
                md.despublicar(r, token)
            flash(request, "Fluxo nativo apagado no Paperless; a regra continua no Gestor.")
        elif acao == "restaurar":
            ver = db.scalar(select(RegraDocumentoVersao).where(RegraDocumentoVersao.regra_id == r.id,
                                                               RegraDocumentoVersao.versao == _int(form.get("versao"))))
            if ver:
                md.guardar_regra(db, r, _json.loads(ver.dados), admin, f"repõe a versão {ver.versao}")
                flash(request, f"Reposta a versão {ver.versao} (fica como versão {r.versao}).")
        auditar(db, admin, "paperless_regra", f"{acao}: {r.nome}")
        db.commit()
    except paperless.ErroPaperless as e:
        db.rollback()
        flash(request, str(e), "erro")
    return RedirectResponse(f"/admin/paperless/regras/{rid}", status_code=303)


# ------------------------------------------------------------------ motor: revisão
@router.get("/revisao")
def revisao(request: Request, estado: str = "revisao", admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    ctx, _ = _ctx_motor(request, db, admin)
    estado = estado if estado in md.ESTADOS else "revisao"
    cs = db.scalars(select(ClassificacaoDocumento).where(ClassificacaoDocumento.estado == estado)
                    .order_by(ClassificacaoDocumento.processado_em.desc()).limit(200)).all()
    itens = [(c, md.proposta_de_json(c.proposta) if c.proposta else None) for c in cs]
    return render(request, "admin/paperless_revisao.html", itens=itens, estado=estado, **ctx)


@router.post("/revisao")
async def revisao_acao(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    acao, estado = str(form.get("acao", "")), str(form.get("estado", "revisao"))
    if ":" in acao:                                   # botão de uma linha: «aprovar:12»
        acao, um = acao.split(":", 1)
        ids = [int(um)] if um.isdigit() else []
    else:
        ids = [int(x) for x in form.getlist("sel") if str(x).isdigit()]
    _, token = _token(request, db, admin)
    cs = db.scalars(select(ClassificacaoDocumento).where(ClassificacaoDocumento.id.in_(ids))).all() if ids else []
    try:
        if acao == "criar_regra" and cs:
            s = md.proposta_de_json(cs[0].proposta or "{}").regra_sugerida or {}
            return RedirectResponse("/admin/paperless/regras/nova?" + _urlencode({k: v for k, v in s.items() if v}), status_code=303)
        if not token and acao in ("aprovar", "reprocessar"):
            raise paperless.ErroPaperless("O utilizador escolhido não tem token do Paperless.")
        if acao == "aprovar":
            for c in cs:
                md.aprovar(db, token, c, request.app.state.prefs, admin)
                db.commit()
            flash(request, f"{len(cs)} documento{'s' if len(cs) != 1 else ''} aprovado{'s' if len(cs) != 1 else ''}.")
        elif acao == "ignorar":
            for c in cs:
                c.estado = "ignorado"
            flash(request, f"{len(cs)} ignorado{'s' if len(cs) != 1 else ''}: não voltam a ser propostos pelo ciclo.")
        elif acao == "reprocessar":
            r = md.processar(db, token, request.app.state.prefs, u=admin, ids=[c.paperless_id for c in cs])
            flash(request, f"Reprocessados {r.lidos}: {r.aplicados} aplicados, {r.revisao} para rever, {r.sem_regra} sem regra.")
        auditar(db, admin, "paperless_revisao", f"{acao}: {len(cs)} documentos")
        db.commit()
    except paperless.ErroPaperless as e:
        db.rollback()
        flash(request, str(e), "erro")
    return RedirectResponse(f"/admin/paperless/revisao?estado={estado}", status_code=303)


# ------------------------------------------------------------------ motor: reclassificação em massa
FILTROS_RECL = ("desde", "ate", "tipo", "correspondente", "regra", "limite")


def _ctx_recl(request, db, admin, valores):
    ctx, token = _ctx_motor(request, db, admin)
    ctx.update(v={**valores, "limite": valores.get("limite") or "500"}, regras=db.scalars(select(RegraDocumento).order_by(RegraDocumento.ordem)).all(),
               propostas=None, lidos=0, truncado=False, tipos=[], correspondentes=[])
    if token:
        _carregar(ctx, "tipos", lambda: sorted(pg.listar("document_types", token), key=lambda x: (x.get("name") or "").lower()))
        _carregar(ctx, "correspondentes", lambda: sorted(pg.listar("correspondents", token), key=lambda x: (x.get("name") or "").lower()))
    return ctx, token


@router.get("/reclassificar")
def reclassificar(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    ctx, _ = _ctx_recl(request, db, admin, {})
    return render(request, "admin/paperless_reclassificar.html", **ctx)


@router.post("/reclassificar")
async def reclassificar_acao(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    valores = {k: str(form.get(k, "")).strip() for k in FILTROS_RECL}
    valores.update(substituir=bool(form.get("substituir")), sem_correspondente=bool(form.get("sem_correspondente")))
    ctx, token = _ctx_recl(request, db, admin, valores)
    if not token:
        ctx["erros"].append("O utilizador escolhido não tem token do Paperless.")
        return render(request, "admin/paperless_reclassificar.html", status_code=400, **ctx)
    regra_id = _int(valores["regra"])
    try:
        if form.get("acao") == "aplicar":
            ids = [int(x) for x in form.getlist("sel") if str(x).isdigit()]
            n_docs, n_campos, lote, erros = md.aplicar_lote(db, token, ids, valores["substituir"], regra_id, admin,
                                                            request.app.state.prefs)
            auditar(db, admin, "paperless_reclassificar", f"lote {lote}: {n_docs} documentos, {n_campos} campos")
            db.commit()
            flash(request, f"Reclassificação aplicada: {n_docs} documentos, {n_campos} campos alterados (lote {lote}).")
            for e in erros[:10]:
                flash(request, e, "erro")
            return RedirectResponse(f"/admin/paperless/auditoria?lote={lote}", status_code=303)
        filtros = {"added__date__gte": valores["desde"] or None, "added__date__lte": valores["ate"] or None,
                   "document_type__id": _int(valores["tipo"]), "correspondent__id": _int(valores["correspondente"]),
                   "correspondent__isnull": 1 if valores["sem_correspondente"] else None}
        ctx["propostas"], ctx["lidos"], ctx["truncado"] = md.simular(
            db, token, filtros, valores["substituir"], regra_id, min(max(_int(valores["limite"], 500), 1), 5000))
    except paperless.ErroPaperless as e:
        ctx["erros"].append(str(e))
    return render(request, "admin/paperless_reclassificar.html", status_code=400 if ctx["erros"] else 200, **ctx)


# ------------------------------------------------------------------ motor: auditoria
@router.get("/auditoria")
def auditoria(request: Request, lote: str = "", documento: str = "", admin: Utilizador = Depends(exigir_admin),
              db: Session = Depends(get_db)):
    ctx, token = _ctx_motor(request, db, admin)
    ctx["aud"] = md.auditoria(db, token)
    q = select(AlteracaoDocumento).order_by(AlteracaoDocumento.em.desc(), AlteracaoDocumento.id.desc())
    if lote:
        q = q.where(AlteracaoDocumento.lote == lote)
    if _int(documento):
        q = q.where(AlteracaoDocumento.paperless_id == _int(documento))
    nomes_regras = {r.id: r.nome for r in db.scalars(select(RegraDocumento))}
    return render(request, "admin/paperless_auditoria.html", registo=db.scalars(q.limit(300)).all(), lote=lote,
                  documento=documento, nomes_regras=nomes_regras, **ctx)
