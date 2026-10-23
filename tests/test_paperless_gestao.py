"""Administração › Paperless: correspondentes, fluxos, email e manutenção dos documentos (equivalentes aos scripts
PowerShell). Funções de extração testadas com textos sintéticos; as páginas com um Paperless simulado em memória."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import paperless
from app import paperless_gestao as pg
from app import paperless_manutencao as pm
from app.main import create_app
from tests.test_app import PW, _csrf, _post


# ------------------------------------------------------------------ extração (sem Paperless)
def test_total_niveis_e_formatos():
    assert pm.encontrar_total("Subtotal 10,00\nIVA 2,30\nTotal a pagar 12,30 €")["valor"] == 1230
    t = pm.encontrar_total("Total 99,00\nValor total: 1.234,56 EUR")
    assert t["valor"] == 123456 and t["confianca"] == "alta"
    # «total» genérico: ignora linhas de IVA/subtotal, confiança baixa
    t = pm.encontrar_total("Sub-total 5,00\nTotal s/ IVA 5,00\nTotal 6,15")
    assert t["valor"] == 615 and t["confianca"] == "baixa"
    # importe na linha seguinte e com ponto decimal
    assert pm.encontrar_total("TOTAL DA FATURA\n7.20")["valor"] == 720
    assert pm.encontrar_total("Total a pagar 1 234,50")["valor"] == 123450
    # texto fora de ordem: importe sozinho umas linhas antes
    t = pm.encontrar_total("Cliente X\n 45,90 €\nReferência 123\nTotal a pagar")
    assert t["valor"] == 4590 and t["confianca"] == "media"
    assert pm.encontrar_total("sem nada aqui") is None and pm.encontrar_total(None) is None


def test_detetar_data():
    hoje = date(2026, 10, 23)
    assert pm.detetar_data("Vencimento: 30-11-2026\nData de emissão: 05/10/2026", hoje=hoje) == (date(2026, 10, 5), "etiqueta «Data:»")
    # sem etiqueta: ignora linhas de limite/vencimento e datas futuras ou antigas
    d, metodo = pm.detetar_data("Pagar até 31-10-2026\nAssinado 1999-01-01\nLisboa, 12.09.2026", hoje=hoje)
    assert d == date(2026, 9, 12) and metodo == "primeira data do texto"
    assert pm.detetar_data("Data: 31/02/2026") is None


def test_titulo_de():
    assert pm.titulo_de(pm.MODELO_TITULO, "2026-10-05", "", "Fatura", "Vodafone") == "2026-10 Fatura Vodafone"
    assert pm.titulo_de("{ano}-{mes}  {correspondente}", "2026-10-05T00:00", "out 2026", "", "EDP") == "2026-10 EDP"
    assert pm.titulo_de("{periodo} {tipo}", "2026-10-05", "Setembro", "Recibo", "") == "Setembro Recibo"


def test_agrupar_duplicados():
    texto = " ".join(f"palavra{i}" for i in range(40))
    docs = [
        {"id": 1, "title": "A", "created": "2026-09-01", "added": "2026-09-02", "content": texto, "custom_fields": []},
        {"id": 2, "title": "A (reenvio)", "created": "2026-09-01", "added": "2026-09-10", "content": texto + "!!", "custom_fields": []},
        {"id": 3, "title": "B", "created": "2026-08-01", "added": "2026-08-02", "content": "outra coisa " * 30,
         "custom_fields": [{"field": 9, "value": "EUR10.00"}]},
        {"id": 4, "title": "B2", "created": "2026-08-01", "added": "2026-08-03", "content": "diferente " * 30,
         "custom_fields": [{"field": 9, "value": "EUR10.00"}]},
        {"id": 5, "title": "C", "created": "2026-08-01", "added": "2026-08-04", "content": "mais um " * 30,
         "custom_fields": [{"field": 9, "value": "EUR11.00"}]},
    ]
    grupos = pm.agrupar_duplicados(docs, 9)
    ids = [[x["id"] for x in g] for g in grupos]
    assert ids == [[3, 4], [1, 2]]
    assert grupos[1][0]["manter"] and not grupos[1][1]["manter"]
    assert "texto idêntico" in grupos[1][1]["motivos"] and "mesma data e mesmo valor" in grupos[0][0]["motivos"]


def test_problemas_regra_email():
    p, corr = pg._problemas_regra({"action": 3, "action_parameter": "", "enabled": True, "account": 1, "maximum_age": 30,
                                   "filter_from": "x.pt"}, {1})
    assert corr and any("erro 400" in x for x in p)
    p, corr = pg._problemas_regra({"action": 2, "action_parameter": None, "enabled": False, "account": 7, "maximum_age": 0}, {1})
    assert not corr and len(p) == 5


# ------------------------------------------------------------------ páginas (Paperless simulado)
@pytest.fixture
def falso(monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-admin")
    e = {"seq": 500, "chamadas": [], "dados": {
        "correspondents": [{"id": 1, "name": "EDP", "matching_algorithm": 0, "match": "", "is_insensitive": True, "document_count": 1},
                           {"id": 2, "name": "EDP Comercial", "matching_algorithm": 3, "match": "EDP", "is_insensitive": True,
                            "document_count": 2}],
        "document_types": [{"id": 10, "name": "Fatura"}],
        "custom_fields": [{"id": 20, "name": "Valor"}, {"id": 21, "name": "Período"}],
        "tags": [{"id": 30, "name": "Casa"}],
        "storage_paths": [], "users": [{"id": 3, "username": "sergio"}],
        "workflows": [{"id": 40, "name": "Entrada EDP", "order": 5, "enabled": True,
                       "triggers": [{"id": 41, "type": 2, "sources": [1, 3], "matching_algorithm": 3, "match": "EDP", "is_insensitive": True}],
                       "actions": [{"id": 42, "type": 1, "assign_correspondent": 2, "assign_tags": [30], "assign_owner": 3}]}],
        "workflow_triggers": [{"id": 41}],
        "mail_accounts": [{"id": 50, "name": "Gmail Lar", "account_type": 1, "imap_server": "imap.gmail.com", "imap_port": 993,
                           "imap_security": 1, "username": "x@gmail.com"}],
        "mail_rules": [{"id": 60, "name": "EDP", "account": 50, "folder": "INBOX", "filter_from": "edp.pt", "action": 3,
                        "action_parameter": "", "maximum_age": 30, "enabled": True, "order": 1, "consumption_scope": 1}],
        "processed_mail": [{"processed": "2026-10-22T10:00:00", "rule": 60, "status": "FAILED", "subject": "Fatura", "error": "falhou"}],
        "documents": [
            {"id": 100, "title": "fatura edp", "created": "2026-10-05", "added": "2026-10-06", "document_type": 10,
             "correspondent": 1, "content": "Data: 05-10-2026\nTotal a pagar 45,90 €", "custom_fields": [{"field": 21, "value": "out"}], "tags": [30]},
            {"id": 101, "title": "sem total", "created": "1970-01-01", "added": "2026-10-07", "document_type": 10,
             "correspondent": 2, "content": "Emitido em 10/09/2026 nada mais", "custom_fields": [], "tags": []},
            {"id": 102, "title": "com valor", "created": "2026-10-01", "added": "2026-10-08", "document_type": 10,
             "correspondent": 2, "content": "Total 3,00", "custom_fields": [{"field": 20, "value": "EUR3.00"}], "tags": []},
        ]}}

    def pedido(caminho, params=None, binario=False, token=None):
        partes = caminho.strip("/").split("/")
        ep = partes[1]
        if ep == "ui_settings":
            return {"user": {"id": 3, "username": "sergio"}}
        itens = e["dados"].get(ep, [])
        if len(partes) > 2:
            return next(dict(x) for x in itens if x["id"] == int(partes[2]))
        p = params or {}
        res = list(itens)
        if p.get("name__iexact"):
            res = [x for x in res if x["name"].lower() == p["name__iexact"].lower()]
        if p.get("correspondent__id"):
            res = [x for x in res if x.get("correspondent") == int(p["correspondent__id"])]
        if p.get("document_type__id__in"):
            res = [x for x in res if str(x.get("document_type")) in p["document_type__id__in"].split(",")]
        n, pag = int(p.get("page_size", 100)), int(p.get("page", 1))
        pagina = res[(pag - 1) * n: pag * n]
        return {"count": len(res), "results": [dict(x) for x in pagina], "next": "http://x/next" if pag * n < len(res) else None}

    def enviar(metodo, caminho, corpo, *, token):
        partes = caminho.strip("/").split("/")
        ep = partes[1]
        e["chamadas"].append((metodo, caminho, corpo))
        lista = e["dados"].setdefault(ep, [])
        if ep == "documents" and len(partes) > 2 and partes[2] == "bulk_edit":
            for d in e["dados"]["documents"]:
                if d["id"] in corpo["documents"]:
                    d["correspondent"] = corpo["parameters"]["correspondent"]
            return {"result": "OK"}
        if metodo == "POST":
            e["seq"] += 1
            novo = {"id": e["seq"], **corpo}
            lista.append(novo)
            return novo
        oid = int(partes[2])
        alvo = next(x for x in lista if x["id"] == oid)
        if metodo == "DELETE":
            lista.remove(alvo)
            return {}
        alvo.update(corpo)
        return alvo

    monkeypatch.setattr(paperless, "_pedido", pedido)
    monkeypatch.setattr(paperless, "enviar", enviar)
    return e


@pytest.fixture
def admin(tmp_path, falso):
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    return c


def _acao(c, pagina, **dados):
    t = c.get(f"/admin/paperless/{pagina}")
    return c.post(f"/admin/paperless/{pagina}", data={"csrf": _csrf(t), **dados}, follow_redirects=True)


def test_listar_pagina_a_pagina(falso):
    falso["dados"]["tags"] = [{"id": i, "name": f"t{i}"} for i in range(250)]
    assert len(pg.listar("tags", "tk")) == 250
    assert len(pg.listar("tags", "tk", maximo=120)) == 120


def test_correspondentes_editar_criar_fundir_apagar(admin, falso):
    r = admin.get("/admin/paperless/correspondentes")
    assert "EDP Comercial" in r.text and "Texto exato · «EDP»" in r.text
    _acao(admin, "correspondentes", acao="guardar", id="1", nome="EDP SU", algoritmo="3", match="503504564", insensivel="on")
    c1 = next(x for x in falso["dados"]["correspondents"] if x["id"] == 1)
    assert c1["name"] == "EDP SU" and c1["match"] == "503504564" and c1["matching_algorithm"] == 3
    _acao(admin, "correspondentes", acao="guardar", nome="Vodafone", algoritmo="0", match="")
    assert any(x["name"] == "Vodafone" for x in falso["dados"]["correspondents"])
    r = _acao(admin, "correspondentes", acao="fundir", origem="2", destino="1")
    assert "2 documentos mudados" in r.text
    assert {d["correspondent"] for d in falso["dados"]["documents"]} == {1}
    assert not any(x["id"] == 2 for x in falso["dados"]["correspondents"])
    r = _acao(admin, "correspondentes", acao="fundir", origem="1", destino="1")
    assert "dois correspondentes diferentes" in r.text
    _acao(admin, "correspondentes", acao="apagar", id="1", nome="EDP SU")
    assert not any(x["id"] == 1 for x in falso["dados"]["correspondents"])


def test_fluxos_listar_e_alterar(admin, falso):
    r = admin.get("/admin/paperless/fluxos")
    assert "Entrada EDP" in r.text and "Documento adicionado" in r.text and "origem: pasta, email" in r.text
    assert "correspondente EDP Comercial" in r.text and "etiquetas Casa" in r.text and "dono sergio" in r.text
    _acao(admin, "fluxos", acao="desativar", id="40", nome="Entrada EDP")
    assert falso["dados"]["workflows"][0]["enabled"] is False
    _acao(admin, "fluxos", acao="guardar", id="40", nome="Entrada EDP", novo_nome="Entrada EDP SU", ordem="2")
    assert falso["dados"]["workflows"][0]["order"] == 2 and falso["dados"]["workflows"][0]["name"] == "Entrada EDP SU"
    _acao(admin, "fluxos", acao="gatilho", id="40", nome="x", gatilho="41", algoritmo="2", match="edp nif")
    assert ("PATCH", "/api/workflow_triggers/41/", {"matching_algorithm": 2, "match": "edp nif", "is_insensitive": False}) in falso["chamadas"]
    _acao(admin, "fluxos", acao="apagar", id="40", nome="x")
    assert not falso["dados"]["workflows"]


def test_email_rever_e_corrigir(admin, falso):
    r = admin.get("/admin/paperless/email")
    assert "Gmail Lar" in r.text and "erro 400" in r.text and "falhou" in r.text and "Corrigir todas" in r.text
    assert "<td>EDP</td>" in r.text                                              # emails processados: nome da regra
    _acao(admin, "email", acao="corrigir_todas")
    assert falso["dados"]["mail_rules"][0]["action_parameter"] is None
    assert "Corrigir todas" not in admin.get("/admin/paperless/email").text
    _acao(admin, "email", acao="desativar", id="60", nome="EDP")
    assert falso["dados"]["mail_rules"][0]["enabled"] is False


def test_manutencao_valores(admin, falso):
    r = _acao(admin, "manutencao", ferramenta="valores", acao="propor", tipos="Fatura")
    assert "45,90 €" in r.text and "valor não encontrado" in r.text and "1 já certos" in r.text
    assert not any(m == "PATCH" for m, _, _ in falso["chamadas"])               # propor não altera nada
    r = _acao(admin, "manutencao", ferramenta="valores", acao="propor", tipos="Fatura", reprocessar="on")
    assert "com valor" not in r.text and "1 já certos" in r.text                  # recalculado e igual: não é proposto
    t = admin.get("/admin/paperless/manutencao")
    admin.post("/admin/paperless/manutencao", data={"csrf": _csrf(t), "ferramenta": "valores", "acao": "aplicar",
                                                    "sel": ["100", "101"], "v_100": "EUR45.90", "v_101": "rever"})
    docs = {d["id"]: d for d in falso["dados"]["documents"]}
    assert {"field": 20, "value": "EUR45.90"} in docs[100]["custom_fields"]
    assert {"field": 21, "value": "out"} in docs[100]["custom_fields"]                # o outro campo fica
    tag = next(x["id"] for x in falso["dados"]["tags"] if x["name"] == pm.ETIQUETA_REVER)
    assert tag in docs[101]["tags"] and tag not in docs[100]["tags"]


def test_manutencao_datas_titulos_diagnostico(admin, falso):
    r = _acao(admin, "manutencao", ferramenta="datas", acao="propor")
    assert "10/09/2026" in r.text and "1970-01-01" in r.text and "2 já certos" in r.text
    t = admin.get("/admin/paperless/manutencao")
    admin.post("/admin/paperless/manutencao", data={"csrf": _csrf(t), "ferramenta": "datas", "acao": "aplicar",
                                                    "sel": "101", "v_101": "2026-09-10"})
    assert next(d for d in falso["dados"]["documents"] if d["id"] == 101)["created"] == "2026-09-10"
    r = _acao(admin, "manutencao", ferramenta="titulos", acao="propor", modelo="{periodo} {tipo} {correspondente}")
    assert "out Fatura EDP" in r.text and "2026-10 Fatura EDP Comercial" in r.text
    r = _acao(admin, "manutencao", ferramenta="diagnostico", acao="propor", doc="http://192.168.1.232:8300/documents/100/details")
    assert "45,90 €" in r.text and "Total a pagar 45,90" in r.text and "05/10/2026" in r.text


def test_manutencao_duplicados_marca(admin, falso):
    texto = " ".join(f"palavra{i}" for i in range(40))
    falso["dados"]["documents"] += [
        {"id": 200, "title": "orig", "created": "2026-09-01", "added": "2026-09-02", "correspondent": 1, "content": texto, "tags": []},
        {"id": 201, "title": "copia", "created": "2026-09-01", "added": "2026-09-05", "correspondent": 1, "content": texto, "tags": [30]}]
    r = _acao(admin, "manutencao", ferramenta="duplicados", acao="propor", correspondente="1")
    assert "1 grupo em 3 documentos" in r.text and "texto idêntico" in r.text
    t = admin.get("/admin/paperless/manutencao")
    admin.post("/admin/paperless/manutencao", data={"csrf": _csrf(t), "ferramenta": "duplicados", "acao": "aplicar",
                                                    "sel": "201", "v_201": "1"})
    tag = next(x["id"] for x in falso["dados"]["tags"] if x["name"] == pm.ETIQUETA_DUPLICADO)
    docs = {d["id"]: d for d in falso["dados"]["documents"]}
    assert docs[201]["tags"] == [30, tag] and tag not in docs[200]["tags"]


def test_circuito_lar_com_vistas(admin, falso):
    t = admin.get("/admin/paperless?modelo=lar")
    assert "Lar (faturas e recibos)" in t.text and "Correspondentes" in t.text          # separadores novos
    admin.post("/admin/paperless", data={"csrf": _csrf(t), "modelo": "lar", "acao": "aplicar", "utilizador": "1",
                                         "lar": "Santa Casa STC", "filtro": "Santa Casa", "pessoa": "Mãe", "pasta": "Família/Mãe/Lar"})
    d = falso["dados"]
    tags = {x["name"]: x["id"] for x in d["tags"]}
    assert {"Mãe", "Lar", "Mensalidade", "Medicação", "Por pagar", "Pago", "Completo"} <= set(tags)
    assert next(x for x in d["tags"] if x["name"] == "Mensalidade")["match"] == "mensalidade"
    fluxo = next(x for x in d["workflows"] if x["name"] == "Entrada Lar")
    assert fluxo["triggers"][0]["match"] == "Santa Casa"
    assert fluxo["actions"][0]["assign_tags"] == [tags["Mãe"], tags["Lar"], tags["Por pagar"]]
    vistas = {x["name"]: x for x in d["saved_views"]}
    assert vistas["Por pagar"]["filter_rules"] == [{"rule_type": 6, "value": str(tags["Por pagar"])}]
    assert vistas["Mãe - Lar"]["show_in_sidebar"] is True
    assert next(x for x in d["storage_paths"] if x["name"] == "Mãe - Lar")["path"] == "Família/Mãe/Lar/{{ created_year }}/{{ title }}"


def test_sem_token(admin, monkeypatch, falso):
    monkeypatch.delenv("GESTOR_PAPERLESS_TOKEN")
    r = admin.get("/admin/paperless/correspondentes")
    assert "não tem token do Paperless" in r.text
