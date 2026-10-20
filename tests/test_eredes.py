"""E-Redes: Excel de leituras do Balcão Digital (formato real: morada/CPE/contador no topo, leituras acumuladas por
período, mais recente primeiro) → contador ligado à casa pela morada, consumo por mês e €/kWh na comparação de
fornecedores. Ficheiros fictícios gerados aqui."""
import hashlib
import io
from datetime import date, timedelta

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import eredes
from app.main import create_app
from app.models import Bem, Categoria, ContadorEnergia, Movimento
from tests.test_app import PW, _csrf, _post

CPE = "PT0002000099999999XX"


def xlsx(ini: date, fim: date, base=(1000, 200, 500), por_dia=(10, 2, 5), cpe=CPE,
         morada="RUA DE TESTE 1 2625-000 POVOA DE SANTA IRIA") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Leituras"
    for linha in (("Morada", morada), ("CPE", cpe), ("Número do contador", "000001"), (" ",), (" ",), (" ",),
                  ("Tabela de Leituras", " ", " ", " ", " ", "Energia Ativa (kWh)"),
                  ("Data da Leitura", "Tipo de Leitura", "Origem", "Estado", " ", "Vazio", "Ponta", "Cheias")):
        ws.append(list(linha))
    d, linhas = ini, []
    while d <= fim:
        n = (d - ini).days
        linhas.append([d.strftime("%d/%m/%Y"), "Real", "Operador de Rede de Distribuição", "Válida", "Energia consumida"]
                      + [str(b + p * n) for b, p in zip(base, por_dia)])
        d += timedelta(days=1)
    for l in reversed(linhas):                       # o mais recente primeiro, como a E-Redes
        ws.append(l)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    with app.state.SessionLocal() as db:
        db.add_all([Bem(entidade_id=1, tipo="casa", nome="Póvoa", uso="principal"),
                    Bem(entidade_id=1, tipo="casa", nome="Algarve", uso="ferias")])
        luz = Categoria(nome="Eletricidade", natureza="DESPESA")
        db.add(luz)
        db.flush()
        for mes in (3, 4, 5):                        # débitos G9 de cada mês (pagam o consumo do mês anterior)
            d = date(2026, mes, 17)
            db.add(Movimento(conta_id=1, data_mov=d, descricao_original="DÉBITO DIRETO-G9,SA-10006302807", descricao_norm="G9",
                             montante=-(6000 + mes * 1000), moeda="EUR", categoria_id=luz.id,
                             hash_dedup=hashlib.sha256(str(d).encode()).hexdigest()))
        db.commit()
        c.luz = luz.id
    return c


def test_ler_formato_real():
    f = eredes.ler(xlsx(date(2026, 3, 1), date(2026, 4, 30)))
    assert (f.cpe, f.numero, f.periodos) == (CPE, "000001", ["Vazio", "Ponta", "Cheias"])
    assert len(f.leituras) == 61 and f.leituras[0][0] == date(2026, 4, 30) and f.leituras[-1][1] == {"Vazio": 1000, "Ponta": 200, "Cheias": 500}
    with pytest.raises(eredes.FormatoInvalido):
        eredes.ler(b"isto nao e um excel")


def test_importar_ligar_a_casa_e_consumo(app, admin):
    voltar = f"/relatorios/fornecedores?entidade=1&categoria={admin.luz}"
    r = admin.post("/relatorios/fornecedores/leituras", data={"csrf": _csrf(admin.get("/relatorios")), "entidade": "1", "voltar": voltar},
                   files={"ficheiro": ("Leituras.xlsx", xlsx(date(2026, 3, 1), date(2026, 4, 30)))})
    assert f"E-Redes {CPE}: 61 leituras novas (01/03/2026 a 30/04/2026) · casa: Póvoa." in r.text      # pela morada
    with app.state.SessionLocal() as db:
        c = db.scalar(select(ContadorEnergia))
        meses = eredes.consumos(c)
    assert [(m.mes, m.kwh, m.por_periodo, m.completo) for m in meses] == [
        (date(2026, 3, 1), 527, {"Vazio": 310, "Ponta": 62, "Cheias": 155}, True),      # 31 dias × 17 kWh
        (date(2026, 4, 1), 493, {"Vazio": 290, "Ponta": 58, "Cheias": 145}, True)]      # 29 dias (até 30/04)
    # importar outra vez: nada de novo; com mais dias, só as novas
    r = admin.post("/relatorios/fornecedores/leituras", data={"csrf": _csrf(admin.get("/relatorios")), "entidade": "1", "voltar": voltar},
                   files={"ficheiro": ("L.xlsx", xlsx(date(2026, 3, 1), date(2026, 5, 10)))})
    assert "10 leituras novas" in r.text
    # na comparação de fornecedores (uma casa, sem contratos): kWh e €/kWh com o débito do mês seguinte
    t = admin.get(voltar).text
    assert 'id="consumos"' in t and CPE in t and 'data-grafico="consumo"' in t
    assert "Consumo e €/kWh" in t and "0,190" in t          # março: 527 kWh pagos em abril (100,00 €)


def test_ligar_contador_a_outra_casa(app, admin):
    admin.post("/relatorios/fornecedores/leituras", data={"csrf": _csrf(admin.get("/relatorios")), "entidade": "1"},
               files={"ficheiro": ("L.xlsx", xlsx(date(2026, 3, 1), date(2026, 3, 10), cpe="PT0002000011111111AA",
                                                  morada="RUA DO MAR 2 8200-000 ALBUFEIRA"))})
    with app.state.SessionLocal() as db:
        c = db.scalar(select(ContadorEnergia))
        assert c.bem_id is None                       # «Albufeira» não é o nome de nenhuma casa
        algarve = db.scalar(select(Bem.id).where(Bem.nome == "Algarve"))
    r = admin.post(f"/relatorios/fornecedores/contador/{c.id}", data={"csrf": _csrf(admin.get("/relatorios")), "bem": str(algarve)})
    assert "Contador PT0002000011111111AA → Algarve." in r.text
