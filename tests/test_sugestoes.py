"""Movimentos sem categoria agrupados por padrão, regra num clique e sugestões por IA (API simulada)."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import ia
from app.main import create_app
from app.models import Categoria, Movimento, Regra, SugestaoIA
from app.sugestoes import padrao_de
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov


def test_padroes():
    assert padrao_de("COMPRA 6344 ROYAL VERSAILLES LISBOA") == "ROYAL VERSAILLES LISBOA"
    assert padrao_de("TRF P/ SEGURO VIAGEM ON/OFF AV") == "SEGURO VIAGEM"
    assert padrao_de("COMPRA *8440 CASA DO FRANGO") == "CASA DO FRANGO"
    assert padrao_de("DD-1234-MUNICIPIO") == "MUNICIPIO"
    assert padrao_de("12345") == ""


def test_historico_nao_se_engana_com_comissoes_nem_padroes_curtos():
    from datetime import date
    from types import SimpleNamespace as M

    from app.sugestoes import agrupar

    def mov(desc, cat=None):
        return M(descricao_norm=desc.upper(), descricao_original=desc, montante=-100, categoria_id=cat,
                 data_mov=date(2026, 9, 1), conta=M(nome="X"))
    sem = [mov("McDonald's"), mov("COOP")]
    com = [mov("Comissão Revolut (Pagamento com cartão): McDonald's", cat=7),
           mov("COMISSAO TRANSFERENCIA CREDITO AGRICOLA COOP", cat=7)]
    assert all(g.sugestao_id is None for g in agrupar(sem, com))
    g, = agrupar([mov("COMPRA 1234 ROYAL VERSAILLES")], [mov("PASTELARIA ROYAL VERSAILLES", cat=3)])
    assert (g.sugestao_id, g.origem) == (3, "histórico")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    for d in ("2026-09-01", "2026-09-08", "2026-09-15"):
        _mov(c, 1, d, "2,50", "COMPRA 6344 ROYAL VERSAILLES", sentido="saida")
    _mov(c, 1, "2026-09-10", "13,65", "COMPRA 6344 GERTAL CARNAXIDE", sentido="saida")
    _mov(c, 1, "2026-09-11", "20,00", "TRF MB WAY P/ FULANO DE TAL SILVA", sentido="saida")
    _mov(c, 1, "2026-09-12", "3,00", "COMPRA 6344 PASTELARIA ROYAL VERSAILLES", sentido="saida")
    return c


def _cat(app, nome):
    with app.state.SessionLocal() as db:
        return db.query(Categoria).filter_by(nome=nome).first().id


def _forms(html):
    return {m.group(1): m.group(0) for m in re.finditer(
        r'<form method="post" action="/regras/por-classificar/aplicar">.*?name="original" value="([^"]+)".*?</form>', html, re.S)}


def test_pagina_agrupa_e_sugere_do_historico(admin, app):
    # um movimento já categorizado ensina a sugestão para o mesmo padrão
    restaurantes = _cat(app, "Restaurantes e cafés")
    with app.state.SessionLocal() as db:
        m = db.query(Movimento).filter_by(descricao_original="COMPRA 6344 PASTELARIA ROYAL VERSAILLES").one()
        m.categoria_id = restaurantes
        db.commit()
    p = admin.get("/regras/por-classificar").text
    assert "5 movimentos sem categoria" in p and "Por classificar" in p
    f = _forms(p)
    assert set(f) == {"ROYAL VERSAILLES", "GERTAL CARNAXIDE", "FULANO DE TAL SILVA"}
    assert "3 mov." in p and "sugestão: histórico" in f["ROYAL VERSAILLES"]
    assert f'<option value="{restaurantes}" selected>' in f["ROYAL VERSAILLES"]
    assert ">pessoal<" in f["FULANO DE TAL SILVA"] and 'name="criar_regra" value="1" >' in f["FULANO DE TAL SILVA"]
    assert "Sugestões por IA desligadas" in p and "Pedir sugestões à IA" not in p


def test_aplicar_cria_regra_e_classifica(admin, app):
    cafes = _cat(app, "Restaurantes e cafés")
    r = _post(admin, "/regras/por-classificar", "/regras/por-classificar/aplicar", original="ROYAL VERSAILLES",
              padrao="ROYAL VERSAILLES", sentido="SAIDA", categoria_id=str(cafes), criar_regra="1")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        regra = db.query(Regra).filter_by(padrao="ROYAL VERSAILLES").one()
        assert regra.categoria_id == cafes and regra.sentido == "SAIDA"
        movs = db.query(Movimento).filter(Movimento.descricao_original.like("%ROYAL VERSAILLES%")).all()
        assert all(m.categoria_id == cafes and m.regra_id == regra.id for m in movs) and len(movs) == 4
    # o próximo movimento igual é classificado pela regra
    _mov(admin, 1, "2026-09-20", "2,50", "COMPRA 6344 ROYAL VERSAILLES", sentido="saida")
    with app.state.SessionLocal() as db:
        novo = db.query(Movimento).order_by(Movimento.id.desc()).first()
        assert novo.categoria_id == cafes
    # transferência sem regra: só aqueles movimentos, marcados à mão
    _post(admin, "/regras/por-classificar", "/regras/por-classificar/aplicar", original="FULANO DE TAL SILVA",
          padrao="FULANO DE TAL SILVA", sentido="SAIDA", categoria_id="T")
    with app.state.SessionLocal() as db:
        m = db.query(Movimento).filter(Movimento.descricao_original.like("%FULANO%")).one()
        assert m.transferencia and m.qualificado_por == "manual"
        assert db.query(Regra).filter_by(padrao="FULANO DE TAL SILVA").count() == 0
    assert "FULANO DE TAL SILVA" not in _forms(admin.get("/regras/por-classificar").text)


def test_sugestoes_ia(admin, app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")
    enviado = {}
    energia = _cat(app, "Restaurantes e cafés")

    def falso(sistema, mensagem):
        enviado["msg"] = mensagem
        linhas = re.findall(r"^(\d+)\. \[despesa\] (.+)$", mensagem, re.M)
        return "Aqui está:\n" + json.dumps([
            {"i": int(i), "comerciante": t.title(), "atividade": "pastelaria" if "ROYAL" in t else "cantina",
             "categoria_id": energia if "ROYAL" in t else 99999, "confianca": 0.9} for i, t in linhas])
    monkeypatch.setattr(ia, "_chamar", falso)
    p = admin.get("/regras/por-classificar").text
    assert "Pedir sugestões à IA (3)" in p                          # o MB WAY não conta
    r = _post(admin, "/regras/por-classificar", "/regras/por-classificar/ia")
    assert r.status_code == 303
    assert "ROYAL VERSAILLES" in enviado["msg"] and "FULANO" not in enviado["msg"] and "2,50" not in enviado["msg"]
    with app.state.SessionLocal() as db:
        cache = {s.chave: s for s in db.query(SugestaoIA)}
        assert cache["ROYAL VERSAILLES|SAIDA"].categoria_id == energia
        assert cache["GERTAL CARNAXIDE|SAIDA"].categoria_id is None   # id inválido é descartado
    p = admin.get("/regras/por-classificar").text
    f = _forms(p)
    assert "sugestão: IA" in f["ROYAL VERSAILLES"] and "✦ Royal Versailles — pastelaria" in f["ROYAL VERSAILLES"]
    assert "Pedir sugestões à IA" in p and "disabled" in p.split("Pedir sugestões à IA")[0][-80:]
    # 2.º pedido: nada de novo (cache)
    enviado.clear()
    _post(admin, "/regras/por-classificar", "/regras/por-classificar/ia")
    assert not enviado


def test_erro_da_api(admin, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")

    def falha(sistema, mensagem):
        raise ia.ErroIA("A API respondeu 401: invalid x-api-key")
    monkeypatch.setattr(ia, "_chamar", falha)
    _post(admin, "/regras/por-classificar", "/regras/por-classificar/ia")
    assert "Sugestões da IA indisponíveis: A API respondeu 401" in admin.get("/regras/por-classificar").text
