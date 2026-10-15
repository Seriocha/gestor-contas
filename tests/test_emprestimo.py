"""Empréstimo à habitação: extrato do banco, ligação ao débito, estimativas e página."""
import io
from datetime import date

import pytest
import xlwt
from fastapi.testclient import TestClient

from app import emprestimos as emp
from app.main import create_app
from app.models import Conta, Movimento
from app.parsers import ler_extrato
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import _importar, xls_santander


def xls_emprestimo(prestacoes) -> bytes:
    """prestacoes: (data 'dd-mm-aaaa', n.º, juros, capital, em dívida após), mais recente primeiro —
    duas linhas por prestação, como o Santander exporta."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Consulta Movimentos Empréstimo")
    for i, t in enumerate(["Quinta-feira, 15 de Outubro de 2026", "TITULAR FICTICIO", "",
                           "Consulta Movimentos Empréstimo", "", "Listagem Empréstimos Disponíveis"]):
        ws.write(i, 0, t)
    for j, t in enumerate(["Data Movimento", "Data Valor", "Número Prestação", "Descrição", "Montante",
                           "Saldo em Dívida"]):
        ws.write(6, j, t)
    r = 7
    for d, n, juros, capital, divida in prestacoes:
        for desc, v, s in (("PRESTACAO                      - JUROS", -juros, 0.0),
                           ("PRESTACAO                      - CAPIT.", -capital, divida)):
            for j, x in enumerate([d, d, str(n), desc, v, s]):
                ws.write(r, j, x)
            r += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# Valores reais do extrato (prestações 354 a 356)
EXTRATO = [
    ("25-09-2026", 356, 28.26, 578.22, 21320.39),
    ("25-08-2026", 355, 29.01, 577.47, 21898.61),
    ("27-07-2026", 354, 26.67, 578.24, 22476.08),
]
# Débitos da prestação na conta à ordem (mais recente primeiro); outubro ainda não está no extrato.
ORDEM = [
    ("27-10-2026", "PRESTACAO EMPRESTIMO", -606.48, 3393.52),
    ("25-09-2026", "PRESTACAO EMPRESTIMO", -606.48, 4000.00),
    ("25-08-2026", "PRESTACAO EMPRESTIMO", -606.48, 4606.48),
    ("27-07-2026", "PRESTACAO EMPRESTIMO", -604.91, 5212.96),
    ("01-07-2026", "Vencimento", 2000.0, 5817.87),
]


def test_formulas():
    assert emp.prestacao(10_000_000, emp.parse_taxa("3"), 360) == 42160  # 100.000 € a 3% em 30 anos
    assert emp.parse_taxa("1,54895") == 154895 and emp.fmt_taxa(154895) == "1,54895%"
    assert emp.juros(2132039, emp.parse_taxa("1,54895"), 30) == 2752


def test_parser_emprestimo():
    e = ler_extrato("descarga.xls", xls_emprestimo(EXTRATO))
    assert e.parser == "santander_emprestimo" and e.emprestimo and not e.avisos
    p356, j356 = e.movimentos[-2], e.movimentos[-1]
    assert (p356.montante, j356.montante, j356.saldo) == (60648, -2826, -2132039)
    assert p356.descricao.startswith("PRESTACAO 356") and j356.descricao == "JUROS PRESTACAO 356"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    r = _post(c, "/contas/nova?entidade=1", nome="Crédito habitação", tipo="EMPRESTIMO", moeda="EUR",
              instituicao_id="1", conta_pagamento_id="1", dia_pagamento="25", emp_prazo="392",
              emp_taxa="1,54895", emp_tipo_taxa="variavel", emp_indexante="Euribor 12M",
              emp_padrao="PRESTACAO|EMPRESTIMO")
    assert r.status_code == 303, r.text
    return c


def _movs(app, conta_id):
    with app.state.SessionLocal() as db:
        return [(m.descricao_original, m.montante, m.transferencia, m.categoria.nome if m.categoria else None, m.par_id)
                for m in db.query(Movimento).filter_by(conta_id=conta_id).order_by(Movimento.data_mov, Movimento.ordem)]


def test_extrato_depois_a_ordem(admin, app):
    previa, r = _importar(admin, "emp.xls", xls_emprestimo(EXTRATO), conta_id="2")
    assert r.status_code == 303, previa.text
    with app.state.SessionLocal() as db:
        assert emp.capital_em_divida(db, db.get(Conta, 2)) == 2132039
    movs = _movs(app, 2)
    assert movs[-1][:4] == ("JUROS PRESTACAO 356", -2826, False, "Juros do empréstimo")
    assert movs[-2][2] is True  # a prestação é transferência
    previa, r = _importar(admin, "ordem.xls", xls_santander(ORDEM), conta_id="1")
    assert "3 prestação(ões) do empréstimo ligada(s)" in admin.get(r.headers["location"]).text
    ordem = {(d, v): (t, par) for d, v, t, _, par in _movs(app, 1)}
    assert all(t and par for (d, v), (t, par) in ordem.items() if d == "PRESTACAO EMPRESTIMO")
    # outubro (sem extrato ainda) ficou estimado: juros ≈ 21.320,39 × 1,549% × 32/360
    est = [m for m in _movs(app, 2) if "estimad" in m[0]]
    assert est[0][0].startswith("PRESTACAO 357") and est[1][:2] == ("JUROS PRESTACAO 357 (estimados)", -2935)
    # relatório de setembro: só os juros contam como despesa
    pagina = admin.get("/relatorios?periodo=personalizado&de=2026-09-01&ate=2026-09-30").text
    despesas = pagina.split("Despesas por categoria")[1].split("Receitas por categoria")[0]
    assert "28,26" in despesas and "606,48" not in despesas


def test_estimativa_substituida_pelo_extrato(admin, app):
    _importar(admin, "emp.xls", xls_emprestimo(EXTRATO[1:]), conta_id="2")  # extrato só até agosto
    _importar(admin, "ordem.xls", xls_santander(ORDEM), conta_id="1")      # setembro e outubro estimados
    assert sum("estimad" in m[0] for m in _movs(app, 2)) == 4               # P+J de 356 e 357
    previa, r = _importar(admin, "emp2.xls", xls_emprestimo(EXTRATO), conta_id="2")  # chega setembro real
    assert "1 estimativa(s) substituída(s)" in admin.get(r.headers["location"]).text
    movs = _movs(app, 2)
    assert [m[0] for m in movs if "356" in m[0]] == ["PRESTACAO 356 (capital 578,22 € + juros 28,26 €)", "JUROS PRESTACAO 356"]
    assert all(m[4] for m in movs if m[0].startswith("PRESTACAO"))         # todas ligadas ao débito
    with app.state.SessionLocal() as db:
        r = emp.resumo(db, db.get(Conta, 2), date(2026, 10, 30))
        assert (r.ultima_n, r.n_estimadas, r.n_restantes) == (357, 1, 35)


def test_extratos_trocados_sao_recusados(admin):
    previa, r = _importar(admin, "emp.xls", xls_emprestimo(EXTRATO), conta_id="1")
    assert r is None and "extrato de empréstimo" in previa.text
    previa, r = _importar(admin, "ordem.xls", xls_santander(ORDEM), conta_id="2")
    assert r is None and "só se importa o extrato do empréstimo" in previa.text


def test_pagina_painel_taxa_e_acerto(admin, app):
    _importar(admin, "emp.xls", xls_emprestimo(EXTRATO), conta_id="2")
    pagina = admin.get("/contas/2/emprestimo")
    assert pagina.status_code == 200 and "21.320,39 €" in pagina.text and "n.º 357" in pagina.text
    assert "Quadro previsional" in pagina.text
    painel = admin.get("/").text
    assert "Empréstimo (habitação)" in painel.split("<h2>Passivos</h2>")[1]
    admin.post("/contas/2/emprestimo/taxa", data={"csrf": _csrf(pagina), "taxa": "2,1"})
    with app.state.SessionLocal() as db:
        assert db.get(Conta, 2).emprestimo.taxa_e5 == 210000
    admin.post("/contas/2/emprestimo/acertar", data={"csrf": _csrf(pagina), "capital": "21.300,00", "data": "2026-10-01"})
    with app.state.SessionLocal() as db:
        assert emp.capital_em_divida(db, db.get(Conta, 2)) == 2130000
    # o acerto fica fora dos relatórios
    assert "Acerto" not in admin.get("/relatorios?periodo=personalizado&de=2026-10-01&ate=2026-10-31").text


def test_sem_extrato_estima_a_partir_do_capital(admin, app):
    """Sem extrato do empréstimo: capital e última prestação paga indicados à mão."""
    r = _post(admin, "/contas/nova?entidade=1", nome="Outro empréstimo", tipo="EMPRESTIMO", moeda="EUR",
              instituicao_id="1", conta_pagamento_id="1", dia_pagamento="25", emp_prazo="392", emp_ultima="355",
              emp_taxa="1,54895", saldo_inicial="21.898,61", data_saldo_inicial="2026-08-31", emp_padrao="PRESTACAO")
    assert r.status_code == 303
    _importar(admin, "ordem.xls", xls_santander(ORDEM), conta_id="1")
    movs = _movs(app, 3)
    assert movs[0][0].startswith("PRESTACAO 356 (estimada") and movs[2][0].startswith("PRESTACAO 357 (estimada")
    with app.state.SessionLocal() as db:
        cap = emp.capital_em_divida(db, db.get(Conta, 3))
    assert abs(cap - (2189861 - 2 * 60648 + 2826 + 2900)) < 300  # perto do real (juros estimados ≈ reais)
