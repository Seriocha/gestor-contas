"""Ligar 2 movimentos selecionados na lista e classificar o par de uma vez."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Categoria, Movimento
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Activo", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    _mov(c, 1, "2026-09-01", "40,00", "COMPRA LOJA X", sentido="saida")           # 1
    _mov(c, 1, "2026-09-20", "40,00", "DEVOLUCAO LOJA X")                          # 2
    _mov(c, 1, "2026-09-05", "300,00", "TRF P/ ACTIVO", sentido="saida")           # 3
    _mov(c, 2, "2026-09-25", "300,00", "TRF DE SANTANDER")                         # 4 (fora da janela automática)
    _mov(c, 1, "2026-09-06", "10,00", "OUTRA SAIDA", sentido="saida")              # 5
    return c


def _lote(c, ids, **dados):
    p = c.get("/movimentos?entidade=1")
    return c.post("/movimentos-lote", data={"csrf": _csrf(p), "voltar": "/movimentos?entidade=1", "ambito": "selecao",
                                             "ids": [str(i) for i in ids], **dados}, follow_redirects=False)


def test_painel_so_aparece_com_dois(admin):
    p = admin.get("/movimentos?entidade=1").text
    assert 'id="ligar-dois" hidden' in p and 'name="ligar_como"' in p and 'data-montante="-4000"' in p


def test_ligar_como_transferencia(admin, app):
    r = _lote(admin, [3, 4], acao="ligar", ligar_como="T")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        a, b = db.get(Movimento, 3), db.get(Movimento, 4)
        assert (a.par_id, b.par_id) == (4, 3) and a.transferencia and b.transferencia
    assert "Movimentos ligados e classificados: transferência" in admin.get("/movimentos?entidade=1").text


def test_ligar_com_categoria_compra_e_reembolso(admin, app):
    with app.state.SessionLocal() as db:
        roupa = db.query(Categoria).filter(Categoria.pai_id.is_(None), Categoria.natureza == "DESPESA").first()
    _lote(admin, [1, 2], acao="ligar", ligar_como=str(roupa.id))
    with app.state.SessionLocal() as db:
        a, b = db.get(Movimento, 1), db.get(Movimento, 2)
        assert (a.par_id, b.par_id) == (2, 1) and not a.transferencia and not b.transferencia
        assert a.categoria_id == b.categoria_id == roupa.id and a.qualificado_por == "manual"
    # nos relatórios a compra e a devolução anulam-se nessa categoria
    rel = admin.get("/relatorios?entidade=1&periodo=personalizado&de=2026-09-01&ate=2026-09-02").text
    assert roupa.nome in rel
    rel = admin.get("/relatorios?entidade=1&periodo=personalizado&de=2026-09-01&ate=2026-09-30").text
    desp = rel.split("Despesas por categoria")[1].split("Receitas por categoria")[0]
    assert "0,00 €" in desp.split(roupa.nome)[1][:200]


def test_ligar_excluir_e_validacoes(admin, app):
    _lote(admin, [1, 2], acao="ligar", ligar_como="X")
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, 1).excluido and db.get(Movimento, 2).excluido and db.get(Movimento, 1).par_id == 2
    _lote(admin, [3, 5], acao="ligar", ligar_como="T")                   # duas saídas
    assert "Só se ligam uma entrada e uma saída" in admin.get("/movimentos?entidade=1").text
    _lote(admin, [3, 4, 5], acao="ligar", ligar_como="T")                # três
    assert "seleciona exatamente 2 movimentos" in admin.get("/movimentos?entidade=1").text
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, 3).par_id is None


def test_dividir_a_conta(admin, app):
    # jantar pago por mim (80 €) e a parte do amigo devolvida por MB WAY (30 €)
    _mov(admin, 1, "2026-09-12", "80,00", "COMPRA 6344 RESTAURANTE O PATEO", sentido="saida")      # 6
    _mov(admin, 2, "2026-09-12", "30,00", "TRF MB WAY DE AMIGO")                                    # 7
    p = admin.get("/movimentos?entidade=1").text
    assert 'value="D"' in p and 'id="ligar-resumo"' in p
    r = _lote(admin, [6, 7], acao="ligar", ligar_como="D")
    assert r.status_code == 303
    msg = admin.get("/movimentos?entidade=1").text
    assert "a tua parte é 50,00 €" in msg and "Restaurantes e cafés" in msg
    with app.state.SessionLocal() as db:
        saida, entrada = db.get(Movimento, 6), db.get(Movimento, 7)
        assert (saida.par_id, entrada.par_id) == (7, 6) and not saida.transferencia and not entrada.transferencia
        assert saida.categoria.nome == entrada.categoria.nome == "Restaurantes e cafés"
        assert entrada.notas.startswith("Parte de outros na conta dividida: COMPRA 6344 RESTAURANTE")
    # nos relatórios conta só a minha parte
    rel = admin.get("/relatorios?entidade=1&periodo=personalizado&de=2026-09-12&ate=2026-09-12").text
    assert "Despesas</span><strong>50,00 €" in rel


def test_dividir_usa_a_categoria_da_saida(admin, app):
    with app.state.SessionLocal() as db:
        cat = db.query(Categoria).filter(Categoria.nome == "Take-away").one()
        db.get(Movimento, 1).categoria_id = cat.id
        db.commit()
    _lote(admin, [1, 2], acao="ligar", ligar_como="D")
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, 2).categoria.nome == "Take-away" and db.get(Movimento, 1).par_id == 2
