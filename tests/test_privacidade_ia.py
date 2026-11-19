"""O que sai para a Claude API: dados pessoais mascarados no texto dos documentos, valores só em ordem de grandeza, e a
IA do motor de documentos desligada até o administrador a ligar."""
from app import ia, preferencias


def test_mascarar_dados_pessoais():
    t = ("IBAN PT50 0033 0000 4567 8901 2345 6 · ana.silva@gmail.com · Tel: 912 345 678 · +351 213456789 · "
         "cartão 4111 1111 1111 1111 · NISS: 12345678901 · Utente n.º 123456789 · Cartão de Cidadão 12345678 9 ZZ1 · "
         "NIF 501234567 · Total 12,30 € · Data 2026-11-01 · FT 2026/123")
    m = ia.mascarar(t)
    for dado in ("PT50", "0033", "ana.silva", "912 345", "213456789", "4111", "12345678901", "123456789 ", "ZZ1"):
        assert dado not in m, dado
    assert m.count("[telefone]") == 2 and "[IBAN]" in m and "[email]" in m and "[cartão]" in m and m.count("[identificação]") == 3
    # o que é preciso para classificar fica
    assert "NIF 501234567" in m and "12,30" in m and "2026-11-01" in m and "FT 2026/123" in m


def test_escala_valor():
    assert [ia.escala_valor(v) for v in (-350, 1000, -8491, 15000, -99999, 250000, 900000)] == [
        "até 10", "até 10", "50 a 200", "50 a 200", "200 a 1000", "1000 a 5000", "mais de 5000"]


def test_motor_ia_desligada_por_omissao():
    assert preferencias.OMISSAO["motor_ia"] == "0"
