"""Viagens › Detetar viagens: períodos encontrados pela análise cronológica dos gastos (estrangeiro, zonas postais fora
de casa, comerciantes pouco habituais), reservas antes da partida, «Não é viagem», criar a viagem já preenchida e a
revisão pela IA (simulada; só recebe descrições por dia relativo — sem valores nem datas)."""
import hashlib
import json
from datetime import date, timedelta

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
    # o «Criar viagem» é um formulário com as datas detetadas, que podem ser acertadas antes de criar
    cartao = t.split('id="p-20250520-20250524"')[1].split('class="form-inline criar-viagem"')[1].split("</form>")[0]
    assert 'name="data_ini" value="2025-05-20"' in cartao and 'name="data_fim" value="2025-05-24"' in cartao
    f = admin.get("/viagens/nova?entidade=1&nome=Galiza&data_ini=2025-05-19&data_fim=2025-05-25")   # datas acertadas
    assert 'value="2025-05-19"' in f.text and 'value="2025-05-25"' in f.text
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
    assert 'name="nome" value="Algarve' in r.text and 'name="destino" value="Albufeira"' in r.text
    # analisar os que faltam: só o outro período
    admin.post("/viagens/detetar/ia", data={"csrf": _csrf(t), "entidade": "1"})
    assert len(enviados) == 2


# ------------------------------------------------------------------ fotografias do Immich (simulado)
FOTOS = [  # (dia, cidade, país, ficheiro)
    ("2025-05-20", "Muros", "Spain", "IMG_0001.jpg"), ("2025-05-22", "Santiago de Compostela", "Spain", "IMG_0002.jpg"),
    ("2025-05-23", "Santiago de Compostela", "Spain", "IMG_0003.jpg"),
    ("2025-08-10", "Albufeira", "Portugal", "praia.jpg"), ("2025-08-11", "Albufeira", "Portugal", "jantar.jpg"),
    ("2025-08-13", "Lagos", "Portugal", "barco.mp4"), ("2025-08-14", "Lisboa", "Portugal", "regresso.jpg"),
    ("2025-09-06", "Lisboa", "Portugal", "casa.jpg"), ("2025-09-07", "Loures", "Portugal", "jardim.jpg"),
]


@pytest.fixture
def immich_falso(monkeypatch):
    from app import immich
    monkeypatch.setenv("GESTOR_IMMICH_URL", "http://immich_server:2283")
    monkeypatch.delenv("GESTOR_IMMICH_TOKEN", raising=False)
    monkeypatch.delenv("GESTOR_CIDADES_CASA", raising=False)
    pedidos = []

    def falso(caminho, metodo="GET", corpo=None, binario=False, chave=None):
        if not chave:
            raise immich.ErroImmich("Guarda a tua chave da API do Immich em Perfil.")
        pedidos.append(caminho)
        if caminho == "/api/users/me":
            return {"name": "Sérgio"}
        assert caminho == "/api/search/metadata"
        ini, fim = corpo["takenAfter"][:10], corpo["takenBefore"][:10]
        return {"assets": {"nextPage": None, "items": [
            {"id": f"id-{i}", "type": "IMAGE", "originalFileName": nome, "localDateTime": f"{dia}T12:00:00.000Z",
             "exifInfo": {"city": cidade, "country": pais, "latitude": 37.08, "longitude": -8.25}}
            for i, (dia, cidade, pais, nome) in enumerate(FOTOS) if ini <= dia <= fim]}}
    monkeypatch.setattr(immich, "_pedido", falso)
    return pedidos


def _guardar_chave_immich(c):
    r = c.post("/perfil/immich", data={"csrf": _csrf(c.get("/perfil")), "acao": "guardar", "token": "chave-boa"})
    assert "Chave do Immich guardada" in r.text


def test_resumo_fotos():
    from app.immich import Foto, Fotos

    def fotos(*lugares):
        return Fotos([Foto(str(i), "x.jpg", None, c, p) for i, (c, p) in enumerate(lugares)], len(lugares))
    galiza = vd.resumo_fotos(fotos(("Muros", "Spain"), ("Santiago de Compostela", "Spain"), ("Santiago de Compostela", "Spain"),
                                   ("Lisboa", "Portugal")))
    assert galiza.confirmada and galiza.estrangeiro == ["Spain"] and (galiza.destino, galiza.pais) == ("Santiago de Compostela", "Spain")
    assert galiza.motivo == "fotos em Spain"
    algarve = vd.resumo_fotos(fotos(("Albufeira", "Portugal"), ("Albufeira", "Portugal"), ("Lisboa", "Portugal")))
    assert algarve.confirmada and (algarve.destino, algarve.pais) == ("Albufeira", "Portugal") and not algarve.estrangeiro
    casa = vd.resumo_fotos(fotos(("Lisboa", "Portugal"), ("Loures", "Portugal"), ("Albufeira", "Portugal")))
    assert not casa.confirmada and casa.destino == "Albufeira"            # 1 de 3 fora de casa: não chega
    sem_gps = vd.resumo_fotos(fotos((None, None), (None, None)))
    assert sem_gps.total == 2 and not sem_gps.confirmada and not sem_gps.destino and not sem_gps.lugares


def test_resumo_fotos_cidades_casa_configuraveis(monkeypatch):
    from app.immich import Foto, Fotos
    monkeypatch.setenv("GESTOR_CIDADES_CASA", "Albufeira")
    r = vd.resumo_fotos(Fotos([Foto("1", "x.jpg", None, "Albufeira", "Portugal")], 1))
    assert not r.confirmada and not r.destino


def test_fotos_por_periodo(app, admin, immich_falso):
    t = admin.get("/viagens/detetar?entidade=1").text
    assert "fotos-periodo-caixa" not in t                                    # sem chave do Immich: nada muda
    _guardar_chave_immich(admin)
    t = admin.get("/viagens/detetar?entidade=1").text
    assert immich_falso == ["/api/users/me"]                                 # a página não espera pelo Immich
    assert 'data-url="/viagens/detetar/fotos?entidade=1&ini=2025-08-10&fim=2025-08-14"' in t
    assert 'data-url="/viagens/detetar/fotos?entidade=1&ini=2025-05-20&fim=2025-05-24"' in t
    assert "criar-viagem" in t and "carregarFotos" in t
    # Algarve: fotos longe de casa
    f = admin.get("/viagens/detetar/fotos?entidade=1&ini=2025-08-10&fim=2025-08-14").text
    assert "📷 4 fotos" in f and "Albufeira, Portugal <span class=\"nota\">(2)</span>" in f and "Lagos, Portugal" in f
    assert "confirmada pelas fotos" in f and 'data-destino="Albufeira" data-pais="Portugal"' in f
    assert "praia.jpg" not in f
    # Galiza: o destino é o lugar mais fotografado no estrangeiro
    f = admin.get("/viagens/detetar/fotos?entidade=1&ini=2025-05-20&fim=2025-05-24").text
    assert "📷 3 fotos" in f and "confirmada pelas fotos" in f and 'title="fotos em Spain"' in f
    assert 'data-destino="Santiago de Compostela" data-pais="Spain"' in f
    # fotos só em casa: não confirma; sem fotos
    f = admin.get("/viagens/detetar/fotos?entidade=1&ini=2025-09-06&fim=2025-09-07").text
    assert "📷 2 fotos" in f and "confirmada" not in f
    assert "Sem fotos no Immich" in admin.get("/viagens/detetar/fotos?entidade=1&ini=2025-01-10&fim=2025-01-12").text
    # datas inválidas ou entidade alheia
    assert admin.get("/viagens/detetar/fotos?entidade=1&ini=2025-02-30&fim=2025-03-02").status_code == 400
    assert admin.get("/viagens/detetar/fotos?entidade=1&ini=2025-08-14&fim=2025-08-10").status_code == 400
    assert admin.get("/viagens/detetar/fotos?entidade=99&ini=2025-08-10&fim=2025-08-14").status_code == 404


def test_fotos_por_periodo_erro_do_immich(app, admin, immich_falso, monkeypatch):
    from app import immich
    _guardar_chave_immich(admin)

    def em_baixo(*a, **k):
        raise immich.ErroImmich("Sem ligação ao Immich (timeout).")
    monkeypatch.setattr(immich, "_pedido", em_baixo)
    f = admin.get("/viagens/detetar/fotos?entidade=1&ini=2025-08-10&fim=2025-08-14")
    assert f.status_code == 200 and "Não foi possível ler as fotos do Immich: Sem ligação" in f.text


def test_ia_recebe_os_lugares_das_fotos(app, admin, immich_falso, monkeypatch):
    enviados = []

    def falso(sistema, mensagem, ferramentas=None, modelo_ia=None):
        enviados.append(mensagem)
        return json.dumps({"viagem": True, "destino": None, "pais": None, "nome": "Algarve 2025", "motivo": "lazer",
                           "confianca": 0.8, "explicacao": "Praia."})
    monkeypatch.setattr(ia, "_chamar", falso)
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    # sem chave do Immich: a mensagem não fala de fotos
    admin.post("/viagens/detetar/ia", data={"csrf": _csrf(admin.get("/viagens/detetar?entidade=1")), "entidade": "1",
                                            "chave": "20250520-20250524"})
    assert "fotografias" not in enviados[0]
    _guardar_chave_immich(admin)
    r = admin.post("/viagens/detetar/ia", data={"csrf": _csrf(admin.get("/viagens/detetar?entidade=1")), "entidade": "1",
                                                "chave": "20250810-20250814"})
    msg = enviados[1]
    assert "Lugares das fotografias: Albufeira, Portugal (2); Lagos, Portugal (1); Lisboa, Portugal (1)" in msg
    # só cidade e país: nada de ficheiros, datas ou coordenadas
    assert "praia.jpg" not in msg and "2025" not in msg and "37.08" not in msg and "-8.25" not in msg
    # a resposta guardada é encontrada sem perguntar ao Immich (a chave da cache não inclui as fotos)
    assert "Algarve 2025" in r.text
    n = len(immich_falso)
    t = admin.get("/viagens/detetar?entidade=1").text
    assert "Algarve 2025" in t and len(immich_falso) == n
    # a IA não deu destino: o «+ Criar viagem» fica sem destino (é o browser que o completa com as fotos)
    assert 'name="destino" value=""' in t.split('id="p-20250810-20250814"')[1].split('criar-viagem')[1][:600]
