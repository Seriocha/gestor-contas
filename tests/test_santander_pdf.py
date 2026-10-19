"""Santander «Extrato Consolidado» em PDF: leitura, histórico pela pasta partilhada (validação em lote), sem repetir o
que já entrou pelo .xls, entrada pelo Paperless e ligação de cada movimento ao extrato original.
PDFs fictícios gerados nos testes (nunca extratos reais no git); a API do Paperless é simulada."""
import hashlib
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import paperless
from app.main import create_app
from app.models import Importacao, ImportacaoPendente, Movimento
from app.parsers import ler_extrato
from app.parsers.santander_pdf import interpretar
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import xls_santander

IBAN = "PT50 0018 0003 1234 5678 9012 3"


def texto_extrato(ini: str, fim: str, saldo_ini: str, linhas: list[str], saldo_fim: str, quebra: int | None = None) -> list[str]:
    """As linhas de texto de um extrato consolidado (como o pdfplumber as dá)."""
    cab = f"EXTRATO Nº 7 CONTA Nº 0003.12345678901 PERÍODO DE {ini} A {fim} PÁGINA 001/002"
    out = ["Extrato Consolidado", cab, f"IBAN: {IBAN}", "BANCO SANTANDER TOTTA, S.A. - Capital Social",
           "Resumo das Contas", "DEP.OUT ACTIVOS DEPÓSITOS À ORDEM EUR 1.000,00 30-09",
           "Detalhe de Movimentos da Conta à Ordem", "Moeda: EUR", "Data", "Mov Valor Descritivo do Movimento Moeda Valor Saldo",
           f"Saldo Inicial EUR {saldo_ini}"]
    for i, l in enumerate(linhas):
        if quebra is not None and i == quebra:   # mudança de página a meio da tabela, com o rodapé do balcão
            out += ["Continua", "Nota: Qualquer reclamação sobre a informação constante neste documento", "Balcão: CARNAXIDE",
                    cab.replace("001/002", "002/002"), "Data", "Mov Valor Descritivo do Movimento Moeda Valor Saldo", "Continuação"]
        out.append(l)
    out += [f"Saldo Contabilístico Final EUR {saldo_fim}", "Saldo Disponível Final EUR 0,00",
            "Detalhes de Movimentos da Conta Rendimento e Poupança", "Saldo Inicial EUR 0,00",
            "2026-09-25 2026-09-25 CAPITAL 578,22 -21.320,39"]
    return out


def pdf(linhas: list[str]) -> bytes:
    """PDF mínimo (Helvetica, WinAnsi) com uma linha de texto por linha."""
    def esc(t):
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    paginas = [linhas[i:i + 60] for i in range(0, len(linhas), 60)] or [[]]
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", None,
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>"]
    kids = []
    for pag in paginas:
        corpo = "BT /F1 9 Tf 40 800 Td 12 TL " + " ".join(f"({esc(l)}) Tj T*" for l in pag) + " ET"
        s = corpo.encode("cp1252")
        objs.append(b"<</Length %d>>stream\n" % len(s) + s + b"\nendstream")
        cont = len(objs)
        objs.append(b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents %d 0 R/Resources<</Font<</F1 3 0 R>>>>>>" % cont)
        kids.append(len(objs))
    objs[1] = b"<</Type/Pages/Kids[" + b" ".join(b"%d 0 R" % k for k in kids) + b"]/Count %d>>" % len(kids)
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % f for f in offs)
    return out + b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, x)


SET = texto_extrato("2026-09-01", "2026-09-30", "1.000,00", [
    "01-09 01-09 COMPRA *1264 FARMACIA HIGIENICA -21,12 978,88",
    "02-09 02-09 TRF.IMED. P/ FULANO-E5771617 -77,00 901,88",
    "24-09 24-09 VC-VENCIMENTO LIQUIDO 2.321,49 3.223,37",
], "3.223,37", quebra=2)
OUT = texto_extrato("2026-10-01", "2026-10-31", "3.223,37", [
    "05-10 05-10 COMPRA *1264 MERCADONA -31,28 3.192,09",
    "13-10 13-10 COMPRA *1264 SUPERMERCADO X -26,20 3.165,89",
], "3.165,89")


def test_interpretar_periodo_quebra_de_pagina_e_saldos():
    e = interpretar("\n".join(SET))
    assert (e.parser, e.moeda, e.conta_ref, e.dedup_dias) == ("santander_pdf", "EUR", re.sub(r"\s", "", IBAN), 1)
    assert e.periodo == (date(2026, 9, 1), date(2026, 9, 30)) and not e.avisos
    assert [(m.data_mov, m.montante, m.saldo) for m in e.movimentos] == [
        (date(2026, 9, 1), -2112, 97888), (date(2026, 9, 2), -7700, 90188), (date(2026, 9, 24), 232149, 322337)]
    assert e.movimentos[0].descricao == "COMPRA *1264 FARMACIA HIGIENICA"
    # só a conta à ordem: a poupança e o empréstimo ficam de fora
    assert (e.saldo_inicial, e.saldo_final) == (100000, 322337)


def test_interpretar_periodo_que_atravessa_o_ano_e_linha_em_falta():
    texto = texto_extrato("2023-12-30", "2024-01-31", "100,00", [
        "30-12 30-12 COMPRA A -10,00 90,00", "03-01 03-01 COMPRA B -5,00 85,00"], "80,00")
    e = interpretar("\n".join(texto))
    assert [m.data_mov for m in e.movimentos] == [date(2023, 12, 30), date(2024, 1, 3)]
    assert any("não dá o saldo final" in a for a in e.avisos)


def test_ler_extrato_pdf():
    e = ler_extrato("EXTCON202609.pdf", pdf(SET))
    assert e.parser == "santander_pdf" and len(e.movimentos) == 3 and not e.avisos
    assert e.movimentos[2].descricao == "VC-VENCIMENTO LIQUIDO"


# ------------------------------------------------------------------ histórico pela pasta partilhada

@pytest.fixture
def pasta(tmp_path):
    raiz = tmp_path / "extratos"
    (raiz / "Santander").mkdir(parents=True)
    return raiz


@pytest.fixture
def app(tmp_path, pasta, monkeypatch):
    monkeypatch.setenv("GESTOR_IMPORT_DIR", str(pasta))
    for v in ("GESTOR_PAPERLESS_URL", "GESTOR_PAPERLESS_TOKEN", "GESTOR_PAPERLESS_LINK"):
        monkeypatch.delenv(v, raising=False)
    return create_app(tmp_path / "dados", backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1", iban=IBAN)
    _post(c, "/contas/nova?entidade=1", nome="Santander outra", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    return c


def _importar_xls(c, linhas):
    r = c.get("/importar")
    previa = c.post("/importar", data={"csrf": _csrf(r), "conta_id": "1"}, files={"ficheiro": ("descarga.xls", xls_santander(linhas))})
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', previa.text).group(1)
    c.post("/importar/confirmar", data={"csrf": _csrf(previa), "conta_id": "1", "sha": sha, "nome": "descarga.xls"})


def test_historico_pela_pasta_validar_todos_e_extrato_original(admin, app, pasta):
    # outubro já entrou pelo .xls (descrições escritas de outra forma): não se repete
    _importar_xls(admin, [("13-10-2026", "COMPRA 1264 SUPERMERCADO X", -26.2, 3165.89),
                          ("05-10-2026", "COMPRA 1264 MERCADONA", -31.28, 3192.09)])
    (pasta / "Santander" / "EXTCON202609.pdf").write_bytes(pdf(SET))
    (pasta / "Santander" / "EXTCON202610.pdf").write_bytes(pdf(OUT))
    t = admin.get("/importar/validar")
    r = admin.post("/admin/pasta/ler", data={"csrf": _csrf(t)}).text
    assert "Pasta lida: 2 extratos novos por validar" in r
    with app.state.SessionLocal() as db:
        pend = {p.ficheiro: p for p in db.scalars(select(ImportacaoPendente))}
        assert pend["EXTCON202609.pdf"].conta_id == 1 and "n.º de conta" in pend["EXTCON202609.pdf"].deteccao
        assert (pend["EXTCON202609.pdf"].data_ini, pend["EXTCON202609.pdf"].data_fim) == (date(2026, 9, 1), date(2026, 9, 30))
        assert (pend["EXTCON202610.pdf"].n_lidos, pend["EXTCON202610.pdf"].n_novos) == (2, 0)

    t = admin.get("/importar/validar")
    assert "Validar todos com conta (2)" in t.text
    r = admin.post("/admin/pasta/validar-todos", data={"csrf": _csrf(t)}).text
    assert "2 extratos validados: 3 movimentos novos." in r
    assert not (pasta / "Santander" / "EXTCON202609.pdf").exists()
    assert len(list((pasta / "Santander" / "importados").glob("*_EXTCON2026*.pdf"))) == 2
    with app.state.SessionLocal() as db:
        movs = db.scalars(select(Movimento).where(Movimento.conta_id == 1).order_by(Movimento.data_mov)).all()
        assert [m.montante for m in movs] == [-2112, -7700, 232149, -3128, -2620]     # outubro não se repetiu
        imp_set = db.scalar(select(Importacao).where(Importacao.ficheiro_nome == "EXTCON202609.pdf"))
        assert imp_set.origem.startswith("Santander/importados/") and imp_set.paperless_id is None
        farmacia, mercadona = movs[0], movs[3]
        assert farmacia.importacao_id == imp_set.id

    # detalhe: de onde veio (abre o PDF guardado) e, para o que entrou pelo .xls, também o extrato mensal
    d = admin.get(f"/movimentos/{farmacia.id}/detalhe").text
    assert "Importado de" in d and f"/importar/{imp_set.id}/original" in d
    d = admin.get(f"/movimentos/{mercadona.id}/detalhe").text
    assert "descarga.xls" in d and "Também no extrato" in d and "EXTCON202610.pdf" in d
    o = admin.get(f"/importar/{imp_set.id}/original")
    assert o.status_code == 200 and o.headers["content-type"] == "application/pdf" and o.content.startswith(b"%PDF-")
    assert "inline" in o.headers["content-disposition"]


def test_validar_todos_deixa_os_que_tem_avisos(admin, app, pasta):
    mau = texto_extrato("2026-08-01", "2026-08-31", "100,00", ["03-08 03-08 COMPRA B -5,00 95,00"], "80,00")
    (pasta / "Santander" / "EXTCON202608.pdf").write_bytes(pdf(mau))
    (pasta / "Santander" / "EXTCON202609.pdf").write_bytes(pdf(SET))
    admin.post("/admin/pasta/ler", data={"csrf": _csrf(admin.get("/importar/validar"))})
    r = admin.post("/admin/pasta/validar-todos", data={"csrf": _csrf(admin.get("/importar/validar"))}).text
    assert "1 extrato validado: 3 movimentos novos." in r and "1 ficaram por validar à mão" in r
    assert "não dá o saldo final" in r
    with app.state.SessionLocal() as db:
        assert db.scalar(select(ImportacaoPendente).where(ImportacaoPendente.ficheiro == "EXTCON202608.pdf")).estado == "pendente"


# ------------------------------------------------------------------ entrada pelo Paperless

@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-de-teste")
    monkeypatch.setenv("GESTOR_PAPERLESS_LINK", "http://nas:8300")
    ficheiros = {21: pdf(SET), 22: pdf(OUT)}
    pedidos = []

    def falso(caminho, params=None, binario=False, token=None):
        params = dict(params or {})
        pedidos.append((caminho, params))
        if caminho == "/api/tags/":
            return {"results": [{"id": 5, "name": "Extrato bancário"}] if params.get("name__iexact") == "Extrato bancário" else []}
        if caminho == "/api/document_types/":
            return {"results": []}
        if caminho.endswith("/download/"):
            return ficheiros[int(caminho.split("/")[3])]
        if caminho == "/api/documents/" and "checksum__iexact" in params:
            ids = [i for i, f in ficheiros.items() if hashlib.md5(f).hexdigest() == params["checksum__iexact"]]
            return {"results": [{"id": i} for i in ids]}
        if caminho == "/api/documents/" and params.get("tags__id__all") == 5:
            return {"next": None, "results": [
                {"id": 21, "title": "Santander setembro", "created": "2026-09-30", "original_file_name": "EXTCON202609.pdf"},
                {"id": 22, "title": "Santander outubro", "created": "2026-10-31", "original_file_name": "EXTCON202610.pdf"}]}
        return {"results": [], "count": 0}
    monkeypatch.setattr(paperless, "_pedido", falso)
    return ficheiros


def test_extratos_vindos_do_paperless(admin, app, pasta, api):
    # outubro já tinha entrado pela pasta: o Paperless só o liga ao documento
    (pasta / "Santander" / "EXTCON202610.pdf").write_bytes(api[22])
    admin.post("/admin/pasta/ler", data={"csrf": _csrf(admin.get("/importar/validar"))})
    with app.state.SessionLocal() as db:
        pid = db.scalar(select(ImportacaoPendente.id))
    t = admin.get(f"/admin/pasta/{pid}")
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', t.text).group(1)
    admin.post("/importar/confirmar", data={"csrf": _csrf(t), "conta_id": "1", "sha": sha, "nome": "EXTCON202610.pdf",
                                             "pendente": str(pid)})
    with app.state.SessionLocal() as db:
        out = db.scalar(select(Importacao).where(Importacao.ficheiro_nome == "EXTCON202610.pdf"))
        assert out.paperless_id == 22          # encontrado no Paperless pelo MD5 ao validar
        assert out.origem.startswith("Santander/importados/")

    t = admin.get("/importar/validar")
    assert "Extratos no Paperless" in t.text and 'value="Extrato bancário"' in t.text
    r = admin.post("/admin/paperless/ler", data={"csrf": _csrf(t), "nome": "Extrato bancário"}).text
    assert "Paperless lido: 1 extrato novo por validar, 1 já conhecido." in r
    with app.state.SessionLocal() as db:
        p = db.scalar(select(ImportacaoPendente).where(ImportacaoPendente.paperless_id == 21))
        assert (p.pasta, p.conta_id, p.estado) == ("Paperless", 1, "pendente")
    v = admin.get(f"/admin/pasta/{p.id}").text
    assert "Extrato do Paperless" in v and "ficam ligados ao documento no Paperless" in v and "importados/" not in v
    r = admin.post("/admin/pasta/validar-todos", data={"csrf": _csrf(admin.get("/importar/validar"))}).text
    assert "1 extrato validado: 3 movimentos novos." in r
    with app.state.SessionLocal() as db:
        imp = db.scalar(select(Importacao).where(Importacao.ficheiro_nome == "EXTCON202609.pdf"))
        assert imp.paperless_id == 21 and imp.origem is None
        m = db.scalar(select(Movimento).where(Movimento.importacao_id == imp.id))
    d = admin.get(f"/movimentos/{m.id}/detalhe").text
    assert "http://nas:8300/documents/21/details" in d and "no Paperless" in d
    # ler outra vez não repete
    r = admin.post("/admin/paperless/ler", data={"csrf": _csrf(admin.get("/importar/validar")), "nome": "Extrato bancário"}).text
    assert "0 extratos novos por validar, 2 já conhecidos" in r


def test_ligar_importacoes_antigas_ao_paperless(admin, app, pasta, api, monkeypatch):
    monkeypatch.delenv("GESTOR_PAPERLESS_URL")          # importado sem Paperless…
    r = admin.get("/importar")
    previa = admin.post("/importar", data={"csrf": _csrf(r), "conta_id": "1"}, files={"ficheiro": ("EXTCON202609.pdf", api[21])})
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', previa.text).group(1)
    admin.post("/importar/confirmar", data={"csrf": _csrf(previa), "conta_id": "1", "sha": sha, "nome": "EXTCON202609.pdf"})
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")   # …e ligado depois
    t = admin.get("/importar/validar")
    assert "0 de 1 importações já estão ligadas" in t.text
    r = admin.post("/admin/paperless/ligar", data={"csrf": _csrf(t)}).text
    assert "1 importação ligada ao documento do Paperless." in r


def test_conta_sem_iban_e_outras_contas_do_banco(app, pasta):
    """Conta à ordem sem IBAN, ao lado de uma poupança do Santander e de uma conta noutro banco: o PDF vai para a
    conta à ordem, que aprende o IBAN ao validar."""
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="ActivoBank", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Santander poupança", tipo="POUPANCA", moeda="EUR", instituicao_id="1")
    (pasta / "Santander" / "EXTCON202609.pdf").write_bytes(pdf(SET))
    c.post("/admin/pasta/ler", data={"csrf": _csrf(c.get("/importar/validar"))})
    with app.state.SessionLocal() as db:
        p = db.scalar(select(ImportacaoPendente))
        assert p.conta_id == 2 and "única conta" in p.deteccao
    t = c.get(f"/admin/pasta/{p.id}")
    assert '<option value="2" selected>' in t.text
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', t.text).group(1)
    c.post("/importar/confirmar", data={"csrf": _csrf(t), "conta_id": "2", "sha": sha, "nome": "EXTCON202609.pdf",
                                        "pendente": str(p.id)})
    with app.state.SessionLocal() as db:
        from app.models import Conta
        assert db.get(Conta, 2).iban == re.sub(r"\s", "", IBAN)
