"""Detalhe só de leitura de um movimento, com o mapa de ligações (clique no valor)."""
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import (Abastecimento, Bem, Categoria, Etiqueta, ItemRecheio, Manutencao, Movimento, Subscricao,
                        ViaVerde, Viagem)
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov

HOJE = date.today()


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Activo", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    d = (HOJE - timedelta(days=3)).isoformat()
    _mov(c, 1, d, "84,91", "GALP AV REPUBLICA", sentido="saida")
    _mov(c, 1, d, "500,00", "TRF PARA ACTIVO", sentido="saida")
    _mov(c, 2, d, "500,00", "TRF DE SANTANDER")
    return c


def test_valor_abre_o_detalhe_com_as_ligacoes(admin, app):
    with app.state.SessionLocal() as db:
        galp, trf, trf2 = db.scalars(select(Movimento).order_by(Movimento.id)).all()
        carro = Bem(entidade_id=1, tipo="carro", nome="Frontera", matricula="BU07US")
        sub = Subscricao(entidade_id=1, nome="Netflix", valor=1399, data_inicio=HOJE - timedelta(days=90))
        et = Etiqueta(nome="Viagem: Algarve")
        cat = db.scalars(select(Categoria).where(Categoria.pai_id.is_not(None))).first()
        db.add_all([carro, sub, et])
        db.flush()
        db.add(Viagem(entidade_id=1, nome="Algarve", destino="Lagos", data_ini=HOJE - timedelta(days=5),
                      data_fim=HOJE, etiqueta_id=et.id))
        galp.bem_id, galp.bem_manual, galp.subscricao_id, galp.categoria_id = carro.id, True, sub.id, cat.id
        galp.qualificado_por = "manual"
        galp.etiquetas.append(et)
        db.add(Abastecimento(bem_id=carro.id, data=galp.data_mov, litros=41.04, valor=8491, posto="Galp Lagos",
                             guid="g1", movimento_id=galp.id))
        db.add(Manutencao(bem_id=carro.id, data=galp.data_mov, km=18000, itens="Óleo", custo=1000, movimento_id=galp.id))
        db.add(ItemRecheio(entidade_id=1, nome="Máquina de lavar", marca="Bosch", movimento_id=galp.id))
        db.add(ViaVerde(entidade_id=1, matricula="BU07US", data=galp.data_mov, valor=245, hash="h1", movimento_id=galp.id))
        trf.par_id, trf2.par_id, trf.transferencia, trf2.transferencia = trf2.id, trf.id, True, True
        db.commit()
        ids = galp.id, trf.id, trf2.id, cat.nome

    galp_id, trf_id, trf2_id, cat_nome = ids
    lista = admin.get("/movimentos?entidade=1").text
    assert f'class="valor-mov" href="/movimentos/{galp_id}/detalhe?voltar=' in lista

    p = admin.get(f"/movimentos/{galp_id}/detalhe").text
    assert "-84,91 €" in p and "GALP AV REPUBLICA" in p
    for texto in ("Santander", "Família", cat_nome, "escolhida à mão", "Frontera", "escolhido à mão", "Netflix",
                  "associada à mão", "Algarve", "Lagos", "Abastecimento", "41,04 l", "Oficina", "Máquina de lavar",
                  "Via Verde", "1 passagem", "BU-07-US", "Inserido à mão"):
        assert texto in p, texto
    assert 'href="/patrimonio/' in p and 'href="/viagens/' in p and 'href="/subscricoes/' in p
    assert "#Viagem" not in p and ">Etiqueta<" not in p           # a etiqueta da viagem aparece como viagem
    assert f'href="/movimentos/{galp_id}?voltar=' in p             # botão Editar

    p = admin.get(f"/movimentos/{trf_id}/detalhe").text
    assert "Outro lado da transferência" in p and f'href="/movimentos/{trf2_id}/detalhe"' in p and "Activo" in p
    assert "não conta como receita nem despesa" in p

    assert admin.get("/movimentos/9999/detalhe").status_code == 404


def test_subscricao_pelo_padrao(admin, app):
    with app.state.SessionLocal() as db:
        galp = db.scalars(select(Movimento).order_by(Movimento.id)).first()
        db.add(Subscricao(entidade_id=1, nome="Combustível frota", valor=8491, padrao="GALP",
                          data_inicio=HOJE - timedelta(days=60)))
        db.commit()
        mid = galp.id
    p = admin.get(f"/movimentos/{mid}/detalhe").text
    assert "Combustível frota" in p and "pelo texto do extrato" in p
    assert re.search(r"\d+ ligaç(ão|ões)", p)


def test_analise_do_movimento_pela_ia(admin, app, monkeypatch):
    import json as _json
    from app import ia
    with app.state.SessionLocal() as db:
        galp = db.scalars(select(Movimento).order_by(Movimento.id)).first()
        cat = db.scalars(select(Categoria).where(Categoria.nome == "Combustível")).first()
        mid, cat_id = galp.id, cat.id
    monkeypatch.setattr(ia, "disponivel", lambda: False)
    assert "precisa da chave da Claude API" in admin.get(f"/movimentos/{mid}/detalhe").text
    chamadas = []

    def falso(sistema, mensagem, ferramentas=None, modelo_ia=None):
        chamadas.append(ferramentas)
        if ferramentas:                                   # a conta não tem pesquisa web: a app tenta sem ela
            raise ia.ErroIA("Erro 400: web_search not enabled")
        return _json.dumps({"comerciante": "Galp", "atividade": "Posto de combustível", "localizacao": "Av. da República, Lisboa",
                            "site": "https://www.galp.com", "categoria_id": cat_id, "confianca": 0.92,
                            "explicacao": "Abastecimento num posto Galp.", "fontes": ["https://www.galp.com/pt", "nao-e-url"]})
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    monkeypatch.setattr(ia, "_chamar", falso)
    p = admin.get(f"/movimentos/{mid}/detalhe").text
    assert "Analisar com IA" in p and 'class="analise-passos" hidden' in p
    r = admin.post(f"/movimentos/{mid}/analisar", data={"csrf": _csrf(admin.get(f"/movimentos/{mid}/detalhe")), "fragmento": "1"})
    assert r.status_code == 200 and "<!doctype" not in r.text.lower()           # só a secção
    assert "Galp" in r.text and "Posto de combustível" in r.text and "Av. da República" in r.text and "92% de confiança" in r.text
    assert "sem pesquisa na web" in r.text and "nao-e-url" not in r.text and len(chamadas) == 2
    assert "Mensagem" not in r.text
    # aplicar a categoria sugerida
    admin.post(f"/movimentos/{mid}/analise/aplicar", data={"csrf": _csrf(admin.get(f"/movimentos/{mid}/detalhe"))})
    with app.state.SessionLocal() as db:
        m = db.get(Movimento, mid)
        assert m.categoria_id == cat_id and m.qualificado_por == "manual"
    p = admin.get(f"/movimentos/{mid}/detalhe").text
    assert "é a categoria atual" in p and "Voltar a analisar" in p
    # erro da IA: a secção diz porquê
    monkeypatch.setattr(ia, "_chamar", lambda *a, **k: "sem json")
    r = admin.post(f"/movimentos/{mid}/analisar", data={"csrf": _csrf(admin.get(f"/movimentos/{mid}/detalhe")), "fragmento": "1"})
    assert r.status_code == 502 and "Não foi possível analisar" in r.text and "Galp" in r.text    # a análise anterior fica
