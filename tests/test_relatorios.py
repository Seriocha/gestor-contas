"""Qualificação de movimentos (regras, manual, em massa), relatórios e painel."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import relatorios as rel
from app.main import create_app
from app.models import Categoria, Movimento, Regra
from app.qualificacao import compilar
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import _importar, xls_santander

LINHAS = [  # mais recente primeiro
    ("20-10-2025", "TRF P/ CONTA POUPANCA", -200.0, 1268.0),
    ("15-10-2025", "DD-160100-EDP COMERC", -41.28, 1468.0),
    ("13-10-2025", "Aldi Quinta Piedade", -26.72, 1509.28),
    ("12-10-2025", "Loja Romeo", -10.0, 1536.0),
    ("10-10-2025", "VENCIMENTO EMPRESA X", 1500.0, 1546.0),
    ("15-09-2025", "Aldi Quinta Piedade", -46.0, 46.0),
]


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _importar(c, "d.xls", xls_santander(LINHAS))
    return c


def _movs(app):
    with app.state.SessionLocal() as db:
        return {m.descricao_original: (m.categoria.nome_completo if m.categoria else None, m.transferencia, m.qualificado_por)
                for m in db.query(Movimento)}


def test_padroes_curtos_so_palavra_inteira():
    assert compilar("MEO|EDP", "contem").search("DD-123-EDP COMERC")
    assert not compilar("MEO|EDP", "contem").search("LOJA ROMEO")
    assert compilar("CONDOMIN", "contem").search("TRF CONDOMINIO DO PREDIO")


def test_seed_e_regras_na_importacao(admin, app):
    with app.state.SessionLocal() as db:
        assert db.query(Categoria).filter_by(nome="Supermercado").one().pai.nome == "Alimentação"
        assert db.query(Regra).count() > 20
    movs = _movs(app)
    assert movs["DD-160100-EDP COMERC"][0] == "Casa › Eletricidade"
    assert movs["Aldi Quinta Piedade"][0] == "Alimentação › Supermercado"
    assert movs["VENCIMENTO EMPRESA X"][0] == "Salário"
    assert movs["Loja Romeo"][0] is None  # "MEO" não apanha "ROMEO"


def test_relatorio_mes(admin):
    pagina = admin.get("/relatorios?periodo=personalizado&de=2025-10-01&ate=2025-10-31").text
    # receitas 1500; despesas 200 (TRF, sem categoria) + 41,28 + 26,72 + 10 = 278,00
    assert "1.500,00 €" in pagina and "278,00 €" in pagina
    assert "Alimentação" in pagina and "Sem categoria" in pagina


def test_transferencia_em_massa_sai_dos_relatorios(admin, app):
    with app.state.SessionLocal() as db:
        trf = db.query(Movimento).filter_by(descricao_original="TRF P/ CONTA POUPANCA").one().id
    r = admin.get("/movimentos")
    r = admin.post("/movimentos-lote", data={"csrf": _csrf(r), "ids": [str(trf)], "acao": "transferencia",
                                             "voltar": "/movimentos"}, follow_redirects=False)
    assert r.status_code == 303
    pagina = admin.get("/relatorios?periodo=personalizado&de=2025-10-01&ate=2025-10-31").text
    assert "78,00 €" in pagina and "278,00 €" not in pagina
    # e não aparece como "sem categoria"
    assert "TRF P/ CONTA" not in admin.get("/movimentos?tipo=sem").text


def test_manual_ganha_as_regras(admin, app):
    with app.state.SessionLocal() as db:
        mid = db.query(Movimento).filter_by(descricao_original="DD-160100-EDP COMERC").one().id
        lazer = db.query(Categoria).filter_by(nome="Lazer").one().id
    r = admin.get("/movimentos")
    assert admin.post(f"/movimentos/{mid}/categoria", data={"csrf": _csrf(r), "categoria_id": str(lazer)}).json()["ok"]
    r = admin.get("/regras")
    admin.post("/regras/aplicar", data={"csrf": _csrf(r)})
    assert _movs(app)["DD-160100-EDP COMERC"][:3:2] == ("Lazer", "manual")


def test_nova_regra_a_partir_de_movimento_e_aplicar(admin, app):
    with app.state.SessionLocal() as db:
        mid = db.query(Movimento).filter_by(descricao_original="Loja Romeo").one().id
        cat = db.query(Categoria).filter_by(nome="Vestuário").one().id
    form = admin.get(f"/regras/nova?mov={mid}")
    assert 'value="LOJA ROMEO"' in form.text and "Corresponde a 1 movimento" in form.text
    r = admin.post("/regras/nova", data={"csrf": _csrf(form), "padrao": "LOJA ROMEO", "modo": "contem",
                                         "categoria_id": str(cat), "prioridade": "10", "ativa": "1", "aplicar": "1"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert _movs(app)["Loja Romeo"][0] == "Compras › Vestuário"


def test_regra_sem_acao_e_regex_invalida(admin):
    form = admin.get("/regras/nova")
    r = admin.post("/regras/nova", data={"csrf": _csrf(form), "padrao": "X", "modo": "contem", "ativa": "1"})
    assert r.status_code == 400 and "pelo menos uma ação" in r.text
    r = admin.post("/regras/nova", data={"csrf": _csrf(form), "padrao": "(", "modo": "regex", "excluir": "1"})
    assert r.status_code == 400 and "inválida" in r.text


def test_apagar_categoria_move_movimentos(admin, app):
    with app.state.SessionLocal() as db:
        alim = db.query(Categoria).filter_by(nome="Alimentação").one().id
        outras = db.query(Categoria).filter_by(nome="Outras despesas").one().id
    r = admin.get("/categorias")
    assert r.status_code == 200
    admin.post(f"/categorias/{alim}/apagar", data={"csrf": _csrf(r), "destino": str(outras)})
    assert _movs(app)["Aldi Quinta Piedade"][0] == "Outras despesas"


def test_painel_e_csv(admin):
    painel = admin.get("/")
    assert painel.status_code == 200 and 'id="dados-graficos"' in painel.text
    assert "Património líquido" in painel.text and "1.268,00 €" in painel.text
    csv = admin.get("/relatorios/csv?periodo=personalizado&de=2025-10-01&ate=2025-10-31")
    assert csv.status_code == 200 and "Salário" in csv.text and "1500,00" in csv.text


def test_saldo_historico_antes_do_primeiro_movimento(admin, app):
    with app.state.SessionLocal() as db:
        hist = rel.HistoricoConta(db, db.query(Movimento).first().conta)
        assert hist.saldo_em(date(2025, 9, 30)) == 4600
        assert hist.saldo_em(date(2025, 10, 12)) == 153600
        assert hist.saldo_em(date(2025, 1, 1)) == 9200  # antes de tudo: saldo antes do 1.º movimento


def test_periodos():
    p = rel.periodo("trimestre", date(2026, 10, 14))
    assert (p.ini, p.fim) == (date(2026, 10, 1), date(2026, 12, 31))
    assert (p.anterior.ini, p.anterior.fim) == (date(2026, 7, 1), date(2026, 9, 30))
    ytd = rel.periodo("ytd", date(2026, 10, 14))
    assert (ytd.anterior.ini, ytd.anterior.fim) == (date(2025, 1, 1), date(2025, 10, 14))


def test_filtros_com_campos_vazios_como_o_browser(admin):
    """Um <select> em "Todas" envia o campo vazio (ex.: conta=&categoria=); não pode dar 422."""
    r = admin.get("/movimentos?entidade=1&conta=&categoria=&etiqueta=&tipo=&q=aldi&de=&ate=")
    assert r.status_code == 200 and "Aldi Quinta Piedade" in r.text and "Loja Romeo" not in r.text
    assert admin.get("/contas/1/movimentos?categoria=&etiqueta=&tipo=&q=&de=&ate=&pagina=").status_code == 200
    r = admin.get("/relatorios?entidade=1&periodo=ano&de=&ate=&conta=&etiqueta=")
    assert r.status_code == 200 and "Relatórios" in r.text
    assert admin.get("/relatorios/csv?entidade=1&periodo=ano&de=&ate=&conta=&etiqueta=").status_code == 200
    assert admin.get("/?entidade=&periodo=mes").status_code == 200
    assert admin.get("/importar?conta=").status_code == 200
    assert admin.get("/regras/nova?mov=").status_code == 200
    assert admin.get("/contas?arquivadas=").status_code == 200


def test_filtro_por_conta_na_lista_global_vai_para_a_conta(admin):
    r = admin.get("/movimentos?entidade=1&conta=1&categoria=&etiqueta=&tipo=&q=&de=&ate=", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/contas/1/movimentos")
