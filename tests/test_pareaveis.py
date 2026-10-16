"""Ecrã «Movimentos pareáveis»: sugestões débito ↔ crédito, ligar, classificar e ignorar."""
import pytest
from fastapi.testclient import TestClient

from app import pareaveis
from app.main import create_app
from app.models import Categoria, Entidade, Movimento, ParIgnorado
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Activo", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    _post(c, "/contas/nova?entidade=1", nome="BiG", tipo="ORDEM", moeda="EUR", instituicao_id="3")
    _post(c, "/contas/nova?entidade=2", nome="CA Mãe", tipo="ORDEM", moeda="EUR", instituicao_id="6")
    _mov(c, 1, "2026-03-03", "10.000,00", "TRF P/ SERGIO", sentido="saida")         # 1
    _mov(c, 1, "2026-03-03", "10.000,00", "TRF P/ SERGIO", sentido="saida")         # 2
    _mov(c, 2, "2026-03-04", "10.000,00", "TRF IMEDIATA A CREDITO")                 # 3
    _mov(c, 2, "2026-03-04", "10.000,00", "TRF IMEDIATA A CREDITO")                 # 4
    _mov(c, 1, "2026-05-10", "55,00", "COMPRA LOJA", sentido="saida")               # 5
    _mov(c, 2, "2026-05-16", "55,00", "DEVOLUCAO")                                  # 6 (6 dias)
    _mov(c, 1, "2026-05-20", "80,00", "QUALQUER", sentido="saida")                  # 7
    _mov(c, 1, "2026-05-20", "80,00", "ENTRADA MESMA CONTA")                        # 8 (mesma conta: não)
    _mov(c, 3, "2026-05-20", "80,00", "CREDITO NOUTRA ENTIDADE")                    # 9 (outra entidade: não)
    return c


def test_candidatos(admin, app):
    with app.state.SessionLocal() as db:
        pares, total = pareaveis.candidatos(db, 1, dias=3)
        assert total == 4 and {p.chave for p in pares} == {"1-3", "1-4", "2-3", "2-4"}
        assert all(p.concorrentes == 2 and p.dias == 1 and p.confianca == "média" for p in pares)
        largos, _ = pareaveis.candidatos(db, 1, dias=7)
        assert "5-6" in {p.chave for p in largos} and "7-8" not in {p.chave for p in largos}
        assert pareaveis.candidatos(db, 1, dias=0)[1] == 0
        assert pareaveis.candidatos(db, 1, dias=7, valor_min=100_00)[1] == 4


def test_ecra_e_ligar(admin, app):
    p = admin.get("/movimentos-pares?entidade=1").text
    assert "4 sugestões" in p and "2 alternativas" in p and "Pareáveis" in admin.get("/movimentos?entidade=1").text
    # liga só o par da linha (botão «Ligar»), mesmo com outros marcados
    r = admin.post("/movimentos-pares/ligar", data={"csrf": _csrf(admin.get("/movimentos-pares?entidade=1")),
                                                    "voltar": "/movimentos-pares?entidade=1", "pares": ["2-4"],
                                                    "so": "1-3", "como": "T"}, follow_redirects=False)
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, 1).par_id == 3 and db.get(Movimento, 3).transferencia
        assert db.get(Movimento, 2).par_id is None
        assert [p.chave for p in pareaveis.candidatos(db, 1, dias=3)[0]] == ["2-4"]
    # seleção com dois pares que partilham um movimento: só o primeiro é ligado
    _post(admin, "/movimentos-pares?entidade=1&dias=7", "/movimentos-pares/ligar", pares=["2-4", "5-6"], como="T")
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, 2).par_id == 4 and db.get(Movimento, 5).par_id == 6


def test_ligar_com_categoria_e_ignorar(admin, app):
    with app.state.SessionLocal() as db:
        cat = db.query(Categoria).filter(Categoria.pai_id.is_(None), Categoria.natureza == "DESPESA").first().id
    _post(admin, "/movimentos-pares?entidade=1&dias=7", "/movimentos-pares/ligar", so="5-6", como=str(cat))
    with app.state.SessionLocal() as db:
        a, b = db.get(Movimento, 5), db.get(Movimento, 6)
        assert a.par_id == 6 and not a.transferencia and a.categoria_id == b.categoria_id == cat
    _post(admin, "/movimentos-pares?entidade=1", "/movimentos-pares/ignorar", pares=["1-3", "1-4"])
    with app.state.SessionLocal() as db:
        assert db.query(ParIgnorado).count() == 2
        assert {p.chave for p in pareaveis.candidatos(db, 1, dias=3)[0]} == {"2-3", "2-4"}
        assert db.get(Movimento, 1).par_id is None


def test_respeita_inicio_da_analise(admin, app):
    with app.state.SessionLocal() as db:
        db.get(Entidade, 1).data_inicio = __import__("datetime").date(2026, 4, 1)
        db.commit()
    p = admin.get("/movimentos-pares?entidade=1&dias=7").text
    assert "1 sugestão" in p and "Só movimentos desde 01/04/2026" in p
    assert "5 sugestões" in admin.get("/movimentos-pares?entidade=1&dias=7&anteriores=1").text


def test_lado_ja_classificado_baixa_a_confianca(admin, app):
    with app.state.SessionLocal() as db:
        antes = {p.chave: p.pontos for p in pareaveis.candidatos(db, 1, dias=7)[0]}
        db.get(Movimento, 5).categoria_id = db.query(Categoria).filter(Categoria.natureza == "DESPESA").first().id
        db.flush()
        depois = {p.chave: p.pontos for p in pareaveis.candidatos(db, 1, dias=7)[0]}
        assert depois["5-6"] == antes["5-6"] - 1 and depois["1-3"] == antes["1-3"]
