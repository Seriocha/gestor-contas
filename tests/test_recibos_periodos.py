"""Importar › Recibos › Períodos no Paperless: grelha ano × mês por empregador, com meses em falta, repetidos, vários
recibos no mesmo mês e subsídios de férias/Natal. O Paperless é simulado em memória (textos sintéticos, sem dados pessoais)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import paperless, recibos_periodos as rp
from app.main import create_app
from tests.test_app import PW, _post
from tests.test_recibos import CAMPOS, DEZ_2025, JAN_2025

NATAL = DEZ_2025.replace("ISENCAO HORARIO", "SUBSIDIO DE NATAL 202512 3.500,00\nISENCAO HORARIO")


def _doc(i, corr, periodo=None, liquido=None, content=""):
    campos = ([{"field": 3, "value": periodo}] if periodo else []) + ([{"field": 8, "value": liquido}] if liquido else [])
    return {"id": i, "title": f"Recibo {i}", "content": content, "correspondent": corr, "custom_fields": campos}


DOCS = [
    _doc(1, 10, "2025-01", "EUR2538.95", JAN_2025),
    _doc(2, 10, "2025-03", "EUR2290.37"),
    _doc(3, 10, "2025-03", "EUR2290.37"),            # o mesmo Líquido → repetido
    _doc(5, 10, "2025-06", "EUR2290.37"),
    _doc(6, 10, "2025-06", "EUR900.00"),             # Líquido diferente → vários (ex.: prémio)
    _doc(4, 10, content=NATAL),                      # sem campo «Período»: lido do texto (2025-12)
    _doc(7, 11, "12/2024", "EUR100.00"),
    _doc(8, None, content=""),                       # sem período nem empregador
]


@pytest.fixture
def falso(monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-admin")
    paginas = []

    def pedido(caminho, params=None, binario=False, token=None):
        if caminho == "/api/document_types/":
            return {"results": [{"id": 4, "name": "Recibo de vencimento"}]}
        if caminho == "/api/custom_fields/":
            return {"results": CAMPOS}
        if caminho == "/api/correspondents/":
            return {"results": [{"id": 10, "name": "Empresa A"}, {"id": 11, "name": "Banco B"}]}
        if caminho == "/api/documents/":
            assert params["document_type__id"] == 4 and token == "token-admin"
            paginas.append(params["page"])
            metade = len(DOCS) // 2
            if params["page"] == 1:
                return {"results": [dict(d) for d in DOCS[:metade]], "next": "pagina 2"}
            return {"results": [dict(d) for d in DOCS[metade:]], "next": None}
        raise AssertionError(caminho)
    monkeypatch.setattr(paperless, "_pedido", pedido)
    return paginas


def test_periodo_de():
    assert [rp.periodo_de(x) for x in ("2025-12", "2025-1", "12/2025", "3-2024", " 2024.07 ")] == \
        ["2025-12", "2025-01", "2025-12", "2024-03", "2024-07"]
    assert [rp.periodo_de(x) for x in ("", None, "2025-13", "dezembro", "202512")] == [None] * 5


def test_subsidios():
    assert rp.subsidios(NATAL) == {"natal"}
    assert rp.subsidios(JAN_2025) == set()               # «subsídio de almoço férias» (negativo) não é o de férias
    assert rp.subsidios("SUBS. FERIAS 202507 3.500,00\nSubsídio de Natal 202507 1.750,00") == {"ferias", "natal"}
    assert rp.subsidios("SUBSIDIO DE FERIAS 202507 -3.500,00") == set()      # estorno


def test_mapa_meses_em_falta_repetidos_e_varios(falso):
    mapas = rp.mapas("token-admin")
    assert falso == [1, 2]                                                  # todas as páginas
    assert [m.empregador for m in mapas] == ["Banco B", "Empresa A", rp.SEM_EMPREGADOR]
    a = mapas[1]
    assert (a.primeiro, a.ultimo, len(a.docs), a.do_texto) == ("2025-01", "2025-12", 6, 1)
    assert [m.periodo for m in a.em_falta] == ["2025-02", "2025-04", "2025-05", "2025-07", "2025-08", "2025-09",
                                                "2025-10", "2025-11"]
    assert [m.periodo for m in a.repetidos] == ["2025-03"] and [m.periodo for m in a.varios] == ["2025-06"]
    (ano, meses), = a.anos
    assert ano == 2025 and len(meses) == 12
    dez = meses[11]
    assert dez.estado == "ok" and dez.docs[0].do_texto and dez.docs[0].liquido == 229037 and dez.siglas == "SN"
    assert meses[0].docs[0].link == "http://paperless:8000/documents/1/details"
    b = mapas[0]
    assert (b.primeiro, b.ultimo, b.em_falta) == ("2024-12", "2024-12", [])
    assert [m.estado for m in b.anos[0][1]] == [""] * 11 + ["ok"]          # fora do intervalo: em branco, não «falta»
    s = mapas[2]
    assert [d.id for d in s.sem_periodo] == [8] and s.anos == [] and s.primeiro is None


def test_varios_anos_e_falta_na_viragem():
    docs = [rp.Doc(1, "", "2023-11"), rp.Doc(2, "", "2024-02")]
    m = rp.mapa("X", docs)
    assert [a for a, _ in m.anos] == [2024, 2023]                          # o mais recente primeiro
    assert [x.periodo for x in m.em_falta] == ["2023-12", "2024-01"]
    assert m.anos[1][1][9].estado == "" and m.anos[0][1][2].estado == ""   # out/2023 e mar/2024 fora do intervalo


def test_repetido_sem_liquido_conhecido():
    m = rp.mapa("X", [rp.Doc(1, "", "2024-05"), rp.Doc(2, "", "2024-05", liquido=100)])
    assert [x.periodo for x in m.repetidos] == ["2024-05"]


def test_meses_desde():
    assert rp.meses_desde("2025-12", date(2026, 2, 10)) == 2 and rp.meses_desde(None) is None


def test_sem_tipo(monkeypatch):
    monkeypatch.setattr(paperless, "_pedido", lambda c, p=None, binario=False, token=None: {"results": []})
    with pytest.raises(paperless.ErroPaperless, match="Recibo de vencimento"):
        rp.mapas("t")


@pytest.fixture
def admin(tmp_path, falso):
    c = TestClient(create_app(tmp_path, backups_automaticos=False))
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    return c


def test_pagina(admin):
    r = admin.get("/importar/recibos")
    assert r.status_code == 200 and "Períodos no Paperless" in r.text and "grelha-recibos" not in r.text
    r = admin.get("/importar/recibos/periodos?utilizador=1")
    assert r.status_code == 200
    t = r.text
    assert "Empresa A" in t and "Banco B" in t and rp.SEM_EMPREGADOR in t
    assert "8 em falta: 2025-02, 2025-04" in t and "1 repetido: 2025-03" in t and "Líquidos diferentes): 2025-06" in t
    assert 'class="r-falta"' in t and 'href="http://paperless:8000/documents/4/details"' in t
    assert "1 com o período lido do texto" in t and ">SN<" in t
    assert "sem meses em falta" in t                                       # Banco B


def test_pagina_sem_token(admin, monkeypatch):
    monkeypatch.delenv("GESTOR_PAPERLESS_TOKEN")
    r = admin.get("/importar/recibos/periodos")
    assert r.status_code == 400 and "não tem token do Paperless" in r.text
