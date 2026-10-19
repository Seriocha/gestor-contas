"""Viagens › Detetar viagens: períodos encontrados pela análise cronológica dos gastos (estrangeiro, zonas postais fora
de casa, comerciantes pouco habituais), reservas antes da partida, «Não é viagem», criar a viagem já preenchida e a
revisão pela IA (simulada; só recebe descrições por dia relativo — sem valores nem datas)."""
import hashlib
import json
from datetime import date, timedelta
from urllib.parse import unquote_plus

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import ia
from app import viagens_detecao as vd
from app.main import create_app
from app.models import Movimento, ViagemDescartada
from tests.test_app import PW, _csrf, _post


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


def _add(db, d: date, desc: str, valor: float, conta_id: int = 1, transferencia: bool = False):
    h = hashlib.sha256(f"{d}{desc}{valor}{conta_id}".encode()).hexdigest()
    db.add(Movimento(conta_id=conta_id, data_mov=d, descricao_original=desc, descricao_norm=desc.upper(),
                     montante=round(-valor * 100), moeda="EUR", hash_dedup=h, transferencia=transferencia))


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    with app.state.SessionLocal() as db:
        # casa: o mesmo supermercado, peixaria e café todos os meses durante 2 anos
        d = date(2024, 1, 3)
        while d < date(2026, 1, 1):
            for i, (desc, v) in enumerate([("COMPRA *1264 LIDL STA IRIA AZOIA 2690-370", 45), ("COMPRA *1264 PEIXARIA ANA PAULA 2625-069", 20),
                                           ("COMPRA *1264 CAFE CENTRAL POVOA 2625-100", 3), ("DÉBITO DIRETO-VODAFONE PORTUGA-0602", 34)]):
                _add(db, d + timedelta(days=i * 3), desc, v)
                _add(db, d + timedelta(days=i * 3 + 14), desc, v)
            d = date(d.year + (d.month == 12), d.month % 12 + 1, 3)
        # Galiza: 5 dias de compras no estrangeiro, com a reserva do hotel um mês antes
        _add(db, date(2025, 4, 18), "COMPRA ESTRANG*1264 BOOKING.COM HOTEL", 320)
        for i, desc in enumerate(["COMPRA ESTRANG*1264 CONFITERIA RIALTO", "COMPRA ESTRANG*1264 TAXI 22 MUROS",
                                  "COMPRA ESTRANG*1264 SIDRERIA LA FINCA", "COMPRA ESTRANG*1264 PARADOR SANTIAGO",
                                  "COMPRA ESTRANG*1264 MERCADO ABASTOS"]):
            _add(db, date(2025, 5, 20) + timedelta(days=i), desc, 25 + i)
        # Algarve: sítios de férias (código postal 82xx, comerciantes raros) durante 5 dias
        for i, desc in enumerate(["COMPRA *1264 PEIXARIA FALESIA 8200-252 ALBU", "COMPRA *1264 RETIRO DO ISCA ALBUFEIRA",
                                  "COMPRA *1264 SOMBRA FRESCA OLHOS DE AGUA", "COMPRA *1264 INTERMARCHE T MEDRON8200-609",
                                  "COMPRA *1264 PINE CLIFFS RESORT"]):
            _add(db, date(2025, 8, 10) + timedelta(days=i), desc, 60)
        # ruído: compras online no estrangeiro e câmbio para ouro (não são viagens)
        for d in (date(2025, 2, 7), date(2025, 3, 12), date(2025, 10, 2)):
            _add(db, d, "COMPRA ESTRANG*1264 AMZN MKTP ES*2T1D860", 30)
        _add(db, date(2025, 11, 5), "Conversão cambial para XAU", 200, transferencia=True)
        db.commit()
    return c


def test_detetar_periodos(app, admin):
    with app.state.SessionLocal() as db:
        ps = vd.detetar(db, 1)
    assert [(p.ini, p.fim) for p in ps] == [(date(2025, 8, 10), date(2025, 8, 14)), (date(2025, 5, 20), date(2025, 5, 24))]
    algarve, galiza = ps
    assert galiza.estrangeiro and galiza.confianca == "alta" and not galiza.zonas
    assert [r.mov.descricao_original for r in galiza.reservas] == ["COMPRA ESTRANG*1264 BOOKING.COM HOTEL"]
    assert "82" in algarve.zonas and not algarve.estrangeiro
    assert "PEIXARIA FALESIA 8200-252 ALBU" in algarve.comerciantes and algarve.gasto == 30000


def test_pagina_criar_e_descartar(app, admin):
    t = admin.get("/viagens/detetar?entidade=1").text
    assert "2 períodos por registar" in t and "confiança alta" in t and "🌍 estrangeiro" in t
    assert 'id="p-20250810-20250814"' in t
    assert "Detetar viagens" in admin.get("/viagens?entidade=1").text
    # o «Criar viagem» leva ao formulário preenchido
    link = unquote_plus(t.split('id="p-20250520-20250524"')[1].split('href="/viagens/nova?')[1].split('"')[0]).replace("&amp;", "&")
    assert "data_ini=2025-05-20" in link and "data_fim=2025-05-24" in link
    f = admin.get("/viagens/nova?" + t.split('id="p-20250520-20250524"')[1].split('href="/viagens/nova?')[1].split('"')[0].replace("&amp;", "&"))
    assert 'value="2025-05-20"' in f.text and 'value="2025-05-24"' in f.text
    r = admin.post("/viagens/nova?entidade=1", data={"csrf": _csrf(f), "nome": "Galiza 2025", "data_ini": "2025-05-20",
                                                    "data_fim": "2025-05-24", "pessoas": "2", "motivo": "lazer"})
    assert r.status_code == 200
    # já registada: deixa de aparecer (a não ser com «mostrar»)
    t = admin.get("/viagens/detetar?entidade=1").text
    assert "1 período por registar" in t and "1 já registado" in t and 'id="p-20250520-20250524"' not in t
    assert "Já registada" in admin.get("/viagens/detetar?entidade=1&todos=1").text
    # «Não é viagem»
    r = admin.post("/viagens/detetar/descartar", data={"csrf": _csrf(admin.get("/viagens/detetar?entidade=1")),
                                                       "entidade": "1", "chave": "20250810-20250814"})
    assert "não volta a ser proposto" in r.text and 'id="p-20250810-20250814"' not in r.text
    with app.state.SessionLocal() as db:
        assert db.scalar(select(ViagemDescartada.data_ini)) == date(2025, 8, 10)


def test_revisao_pela_ia(app, admin, monkeypatch):
    enviados = []

    def falso(sistema, mensagem, ferramentas=None, modelo_ia=None):
        enviados.append(mensagem)
        return json.dumps({"viagem": True, "destino": "Albufeira", "pais": "Portugal", "nome": "Algarve — Albufeira 2025",
                           "motivo": "lazer", "confianca": 0.9, "explicacao": "Restaurantes e praia em Albufeira."})
    monkeypatch.setattr(ia, "_chamar", falso)
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    t = admin.get("/viagens/detetar?entidade=1")
    assert "Analisar com IA" in t.text
    r = admin.post("/viagens/detetar/ia", data={"csrf": _csrf(t), "entidade": "1", "chave": "20250810-20250814"})
    assert "IA: 1 período analisado." in r.text and "Algarve — Albufeira 2025" in r.text and "Albufeira, Portugal" in r.text
    # só descrições por dia relativo: sem valores nem datas
    msg = enviados[0]
    assert "dia 1:" in msg and "PEIXARIA FALESIA" in msg and "2025" not in msg and "60,00" not in msg and "€" not in msg
    # a resposta fica guardada (não volta a perguntar) e preenche a viagem nova
    admin.get("/viagens/detetar?entidade=1")
    assert len(enviados) == 1
    assert "nome=Algarve" in r.text and "destino=Albufeira" in r.text
    # analisar os que faltam: só o outro período
    admin.post("/viagens/detetar/ia", data={"csrf": _csrf(t), "entidade": "1"})
    assert len(enviados) == 2
