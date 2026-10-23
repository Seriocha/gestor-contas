"""Motor de classificação dos documentos do Paperless: extração de atributos, condições das regras, versões,
classificação (confiança, conflitos, atributos em falta), ciclo/«correr agora», revisão, reclassificação em massa
com auditoria, publicação como fluxo nativo, ligação ao movimento bancário e sugestões. Paperless simulado em memória;
textos sintéticos."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import motor_docs as md
from app import paperless
from app.models import (AlteracaoDocumento, ClassificacaoDocumento, Conta, DocumentoLigado, Entidade, Instituicao, Movimento,
                        RegraDocumento, RegraDocumentoVersao)
from tests.test_app import PW, _csrf, _post
from tests.test_paperless_gestao import falso  # noqa: F401 (fixture)

FATURA = """EDP Comercial, S.A.
NIF: 503504564
Fatura FT 2026A1/123456
ATCUD: JFM3KX7P-123456
Data de emissão: 05-10-2026
Período de faturação: 01/09/2026 a 30/09/2026
Data limite de pagamento: 25-10-2026
Entidade: 10611 Referência: 123 456 789
Total IVA 23% 8,58
Total a pagar 45,90 €"""


# ------------------------------------------------------------------ puro
def test_nif_valido():
    assert md.nif_valido("503504564")
    assert not md.nif_valido("503504565") and not md.nif_valido("12345")


def test_extrair_atributos():
    a = md.extrair(FATURA, hoje=date(2026, 10, 23))
    assert a["valor"].valor == "EUR45.90" and a["valor"].confianca == "alta"
    assert a["iva"].valor == "EUR8.58"
    assert a["data_emissao"].valor == "2026-10-05" and a["vencimento"].valor == "2026-10-25"
    assert a["periodo"].valor == "2026-09-01 a 2026-09-30"
    assert a["nif"].valor == "503504564" and a["numero"].valor == "FT 2026A1/123456"
    assert a["atcud"].valor == "JFM3KX7P-123456" and a["referencia_mb"].valor == "10611 123 456 789"
    assert md.extrair("Período: setembro de 2026", ["periodo"])["periodo"].valor == "2026-09"
    assert md.extrair("nada", ["valor", "nif"]) == {}


def test_condicoes():
    t = "Fatura da ÁGUAS do Porto, NIF 503504564"
    assert md.condicao("exato", "aguas do porto", t)
    assert md.condicao("todas", "fatura porto", t) and not md.condicao("todas", "fatura lisboa", t)
    assert md.condicao("qualquer", "lisboa porto", t)
    assert md.condicao("regex", r"NIF\s+50\d{7}", t) and not md.condicao("regex", "(", t)
    assert not md.condicao("todas", "", t) and not md.condicao("todas", "port", t)     # palavra inteira


# ------------------------------------------------------------------ com Paperless simulado
@pytest.fixture
def app_admin(tmp_path, falso):  # noqa: F811
    from app.main import create_app
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    falso["dados"]["documents"] = [
        {"id": 300, "title": "fatura edp", "created": "2026-10-06", "added": "2026-10-06", "document_type": None,
         "correspondent": None, "content": FATURA, "custom_fields": [], "tags": []},
        {"id": 301, "title": "edp sem total", "created": "2026-10-07", "added": "2026-10-07", "document_type": None,
         "correspondent": None, "content": "EDP Comercial NIF: 503504564 aviso", "custom_fields": [], "tags": []},
        {"id": 302, "title": "edp já com outro", "created": "2026-10-08", "added": "2026-10-08", "document_type": 10,
         "correspondent": 2, "content": FATURA, "custom_fields": [], "tags": [30]},
        {"id": 303, "title": "desconhecido", "created": "2026-10-09", "added": "2026-10-09", "correspondent": None,
         "content": "Loja X\nContribuinte: 500000000\nTotal 12,50", "custom_fields": [], "tags": []},
    ]
    return app, c


def _regra(app, **kw) -> RegraDocumento:
    dados = dict(nome="EDP", algoritmo="exato", texto="503504564", correspondente="EDP", tipo="Fatura", etiquetas="Casa, Luz",
                 extrair="valor,data_emissao,vencimento,nif", confianca="alta", ordem=10, ativa=True, so_tipo=None,
                 so_sem_correspondente=False, nota=None)
    dados.update(kw)
    with app.state.SessionLocal() as db:
        r = md.guardar_regra(db, RegraDocumento(), dados)
        db.commit()
        return r


def test_versoes_da_regra(app_admin):
    app, _ = app_admin
    r = _regra(app)
    with app.state.SessionLocal() as db:
        r = db.get(RegraDocumento, r.id)
        md.guardar_regra(db, r, {"texto": "503504564"})             # igual: não sobe versão
        assert r.versao == 1
        md.guardar_regra(db, r, {"texto": "EDP Comercial"}, nota="novo layout")
        db.commit()
        vs = db.scalars(select(RegraDocumentoVersao).where(RegraDocumentoVersao.regra_id == r.id)).all()
        assert r.versao == 2 and [v.versao for v in vs] == [1, 2] and json.loads(vs[0].dados)["texto"] == "503504564"


def test_classificar_confianca_conflito_e_falta(app_admin, falso):  # noqa: F811
    app, _ = app_admin
    r = _regra(app)
    with app.state.SessionLocal() as db:
        regras = md.regras_ativas(db)
        ctx = md.Contexto("tk")
        docs = {d["id"]: d for d in falso["dados"]["documents"]}
        p = md.classificar(docs[300], regras, ctx, hoje=date(2026, 10, 23))
        assert p.automatica and p.regra_id == r.id and p.versao == 1
        campos = {a.campo: a.depois for a in p.alteracoes}
        assert campos["correspondente"] == "EDP" and campos["tipo"] == "Fatura" and campos["etiquetas"] == "Casa, Luz"
        assert campos["campo:Valor"] == "45,90 €" and campos["campo:Data de vencimento"] == "25/10/2026" and campos["data"] == "05/10/2026"
        p = md.classificar(docs[301], regras, ctx)                   # sem valor/datas → revisão
        assert not p.automatica and p.confianca == "media" and "Valor total" in p.motivo
        p = md.classificar(docs[302], regras, ctx)                   # o Paperless já tem outro correspondente
        assert not p.automatica and any("EDP Comercial" in c for c in p.conflitos)
        assert "etiquetas" in {a.campo for a in p.alteracoes} and ("Casa, Casa, Luz" not in str(p.alteracoes))
        p = md.classificar(docs[302], regras, ctx, substituir=True)  # reclassificação forçada
        assert {a.campo for a in p.alteracoes} >= {"correspondente"} and not p.conflitos
        assert md.classificar(docs[303], regras, ctx).origem is None


def test_correr_agora_aplica_revisao_e_auditoria(app_admin, falso):  # noqa: F811
    app, c = app_admin
    _regra(app)
    t = c.get("/admin/paperless/regras")
    assert "Motor de classificação" in t.text and "EDP" in t.text
    r = c.post("/admin/paperless/regras/correr", data={"csrf": _csrf(t)}, follow_redirects=True)
    assert "4 documentos novos lidos · 1 aplicados · 2 para rever · 1 sem regra" in r.text
    docs = {d["id"]: d for d in falso["dados"]["documents"]}
    ids = {ep: {x["name"]: x["id"] for x in falso["dados"][ep]} for ep in ("correspondents", "document_types", "tags", "custom_fields")}
    d = docs[300]
    assert d["correspondent"] == ids["correspondents"]["EDP"] and d["document_type"] == ids["document_types"]["Fatura"]
    assert set(d["tags"]) == {ids["tags"]["Casa"], ids["tags"]["Luz"]} and d["created"] == "2026-10-05"
    assert {"field": ids["custom_fields"]["Valor"], "value": "EUR45.90"} in d["custom_fields"]
    assert {"field": ids["custom_fields"]["NIF do emitente"], "value": "503504564"} in d["custom_fields"]
    with app.state.SessionLocal() as db:
        est = {x.paperless_id: x.estado for x in db.scalars(select(ClassificacaoDocumento))}
        assert est == {300: "aplicado", 301: "revisao", 302: "revisao", 303: "sem_regra"}
        log = db.scalars(select(AlteracaoDocumento).where(AlteracaoDocumento.paperless_id == 300)).all()
        assert {x.campo for x in log} >= {"correspondente", "tipo", "etiquetas", "data", "campo:Valor"}
        assert all(x.versao_regra == 1 and x.lote.startswith("auto-") for x in log)
    # 2.ª vez: nada de novo
    t = c.get("/admin/paperless/regras")
    r = c.post("/admin/paperless/regras/correr", data={"csrf": _csrf(t)}, follow_redirects=True)
    assert "0 documentos novos lidos" in r.text
    # revisão: aprovar o 302 (o conflito fica como estava; aplica as etiquetas e campos)
    r = c.get("/admin/paperless/revisao")
    assert "edp já com outro" in r.text and "o Paperless já tem «EDP Comercial»" in r.text
    with app.state.SessionLocal() as db:
        cid = db.scalar(select(ClassificacaoDocumento.id).where(ClassificacaoDocumento.paperless_id == 302))
    c.post("/admin/paperless/revisao", data={"csrf": _csrf(r), "acao": f"aprovar:{cid}", "estado": "revisao"})
    assert docs[302]["correspondent"] == 2 and ids["tags"]["Luz"] in docs[302]["tags"]
    assert "Auditoria" in c.get("/admin/paperless/auditoria").text


def test_ia_propoe_e_cria_regra(app_admin, falso, monkeypatch):  # noqa: F811
    app, c = app_admin
    from app import ia
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    monkeypatch.setattr(ia, "_chamar", lambda s, m, *a, **k: json.dumps({
        "correspondente": "Loja X", "tipo": "Fatura", "etiquetas": ["Compras"], "valor": "12,50", "data_emissao": "2026-10-01",
        "nif": "500000000", "numero": None, "confianca": 0.8, "motivo": "cabeçalho", "regra": {"algoritmo": "exato", "texto": "Loja X"}}))
    t = c.get("/admin/paperless/regras")
    c.post("/admin/paperless/regras/correr", data={"csrf": _csrf(t)})
    with app.state.SessionLocal() as db:
        cl = db.scalar(select(ClassificacaoDocumento).where(ClassificacaoDocumento.paperless_id == 303))
        assert cl.estado == "revisao" and cl.origem == "ia"
        p = md.proposta_de_json(cl.proposta)
        assert {a.campo for a in p.alteracoes} >= {"correspondente", "tipo", "campo:Valor"} and p.regra_sugerida["texto"] == "Loja X"
    r = c.get("/admin/paperless/revisao")
    assert "Criar a regra sugerida" in r.text
    r = c.post("/admin/paperless/revisao", data={"csrf": _csrf(r), "acao": f"criar_regra:{cl.id}"}, follow_redirects=True)
    assert 'value="Loja X"' in r.text and "Nova regra" in r.text
    # guardar a regra a partir do formulário pré-preenchido
    r = c.post("/admin/paperless/regras/guardar", data={"csrf": _csrf(r), "nome": "Loja X", "algoritmo": "exato", "texto": "Loja X",
                                                        "correspondente": "Loja X", "confianca": "media", "ordem": "50",
                                                        "ativa": "on", "extrair_k": ["valor", "data_emissao"], "origem": "sugestao"},
               follow_redirects=True)
    assert "guardada (versão 1)" in r.text
    with app.state.SessionLocal() as db:
        assert db.scalar(select(RegraDocumento).where(RegraDocumento.nome == "Loja X")).origem == "sugestao"


def test_formulario_valida(app_admin):
    _, c = app_admin
    t = c.get("/admin/paperless/regras/nova")
    r = c.post("/admin/paperless/regras/guardar", data={"csrf": _csrf(t), "nome": "", "algoritmo": "regex", "texto": "(",
                                                        "confianca": "alta"})
    assert r.status_code == 400 and "Indica o nome" in r.text and "Expressão regular inválida" in r.text
    assert "fazer alguma coisa" in r.text


def test_testar_regra_e_repor_versao(app_admin, falso):  # noqa: F811
    app, c = app_admin
    r = _regra(app, ativa=False)
    t = c.get(f"/admin/paperless/regras/{r.id}")
    res = c.post(f"/admin/paperless/regras/{r.id}/acao", data={"csrf": _csrf(t), "acao": "testar", "doc": "300"})
    assert "45,90 €" in res.text and "confiança alta" in res.text
    c.post("/admin/paperless/regras/guardar", data={"csrf": _csrf(t), "id": str(r.id), "nome": "EDP", "algoritmo": "todas",
                                                    "texto": "EDP Comercial", "correspondente": "EDP", "confianca": "alta",
                                                    "ordem": "10", "nota_versao": "layout"})
    with app.state.SessionLocal() as db:
        assert db.get(RegraDocumento, r.id).versao == 2
    t = c.get(f"/admin/paperless/regras/{r.id}")
    assert "layout" in t.text
    c.post(f"/admin/paperless/regras/{r.id}/acao", data={"csrf": _csrf(t), "acao": "restaurar", "versao": "1"})
    with app.state.SessionLocal() as db:
        x = db.get(RegraDocumento, r.id)
        assert x.versao == 3 and x.texto == "503504564"


def test_reclassificar_simular_e_aplicar(app_admin, falso):  # noqa: F811
    app, c = app_admin
    _regra(app)
    t = c.get("/admin/paperless/reclassificar")
    r = c.post("/admin/paperless/reclassificar", data={"csrf": _csrf(t), "acao": "simular", "substituir": "on"})
    assert "3 documentos com alterações ou problemas, de 4 lidos" in r.text
    assert not any(m == "PATCH" for m, _, _ in falso["chamadas"])
    r = c.post("/admin/paperless/reclassificar", data={"csrf": _csrf(t), "acao": "aplicar", "substituir": "on", "sel": ["302"]},
               follow_redirects=True)
    assert "Reclassificação aplicada: 1 documentos" in r.text and "Registo de alterações" in r.text
    docs = {d["id"]: d for d in falso["dados"]["documents"]}
    assert docs[302]["correspondent"] != 2 and docs[300]["correspondent"] is None       # só o marcado
    with app.state.SessionLocal() as db:
        log = db.scalars(select(AlteracaoDocumento)).all()
        assert log and all(x.lote.startswith("lote-") and x.paperless_id == 302 for x in log)
        assert next(x for x in log if x.campo == "correspondente").antes == "EDP Comercial"


def test_publicar_regra_simples(app_admin, falso):  # noqa: F811
    app, c = app_admin
    r = _regra(app, extrair="")
    t = c.get(f"/admin/paperless/regras/{r.id}")
    assert "Publicar no Paperless" in t.text
    c.post(f"/admin/paperless/regras/{r.id}/acao", data={"csrf": _csrf(t), "acao": "publicar"})
    wf = next(x for x in falso["dados"]["workflows"] if x["name"] == "Gestor · EDP")
    assert wf["triggers"][0]["matching_algorithm"] == 3 and wf["triggers"][0]["match"] == "503504564"
    assert wf["actions"][0]["assign_tags"] and wf["actions"][0]["assign_correspondent"]
    # com extração deixa de ser publicável: ao guardar, o fluxo nativo é apagado
    t = c.get(f"/admin/paperless/regras/{r.id}")
    c.post("/admin/paperless/regras/guardar", data={"csrf": _csrf(t), "id": str(r.id), "nome": "EDP", "algoritmo": "exato",
                                                    "texto": "503504564", "correspondente": "EDP", "confianca": "alta",
                                                    "ordem": "10", "ativa": "on", "extrair_k": "valor"})
    assert not any(x["name"] == "Gestor · EDP" for x in falso["dados"]["workflows"])
    with app.state.SessionLocal() as db:
        assert db.get(RegraDocumento, r.id).paperless_fluxo_id is None


def test_ligar_ao_movimento(app_admin, falso):  # noqa: F811
    app, c = app_admin
    _regra(app)
    with app.state.SessionLocal() as db:
        ent = db.scalar(select(Entidade))
        inst = db.scalar(select(Instituicao)) or Instituicao(nome="Banco")
        conta = Conta(entidade=ent, instituicao=inst, nome="Ordem", tipo="ORDEM")
        db.add(conta)
        db.flush()
        db.add(Movimento(conta_id=conta.id, data_mov=date(2026, 10, 24), montante=-4590, descricao_original="DD EDP",
                         descricao_norm="DD EDP", hash_dedup="h1"))
        db.commit()
    t = c.get("/admin/paperless/regras")
    r = c.post("/admin/paperless/regras/correr", data={"csrf": _csrf(t)}, follow_redirects=True)
    assert "1 ligados a movimentos" in r.text
    with app.state.SessionLocal() as db:
        dl = db.scalar(select(DocumentoLigado).where(DocumentoLigado.paperless_id == 300))
        assert dl and dl.movimento_id
        assert db.scalar(select(ClassificacaoDocumento.movimento_id).where(ClassificacaoDocumento.paperless_id == 300)) == dl.movimento_id


def test_sugestoes_por_nif(app_admin, falso):  # noqa: F811
    app, c = app_admin
    falso["dados"]["documents"] = [
        {"id": 400 + i, "title": f"doc {i}", "added": "2026-10-10", "created": "2026-10-10", "correspondent": None,
         "content": "Empresa Y\nNIF: 503504564\nobrigado", "custom_fields": [], "tags": []} for i in range(3)]
    with app.state.SessionLocal() as db:
        ctx = md.Contexto("tk")
        for d in falso["dados"]["documents"]:
            p = md.classificar(d, [], ctx)
            p.atributos = {"nif": {"valor": "503504564"}}
            md._registar(db, p, "sem_regra")
        db.commit()
    r = c.get("/admin/paperless/auditoria")
    assert "Emitente 503504564" in r.text and "3 documentos" in r.text and "No Paperless agora" in r.text
