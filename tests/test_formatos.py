"""Formatos de apresentação: datas em dd/mm/aaaa; a leitura aceita os formatos dos bancos e do browser."""
from datetime import date

from app.formatos import fmt_data, parse_data
from app.relatorios import periodo


def test_datas_mostradas_em_dd_mm_aaaa():
    assert fmt_data(date(2026, 3, 5)) == "05/03/2026"
    assert fmt_data(None) == ""
    assert periodo("personalizado", date(2026, 10, 16), date(2026, 1, 1), date(2026, 2, 28)).rotulo == \
        "01/01/2026 a 28/02/2026"
    assert periodo("ytd", date(2026, 10, 16)).rotulo == "1 jan a 16/10/2026"


def test_leitura_aceita_os_tres_formatos():
    assert parse_data("2026-03-05") == parse_data("05-03-2026") == parse_data("05/03/2026") == date(2026, 3, 5)
