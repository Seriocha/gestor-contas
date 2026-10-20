"""Importar › Recibos (Paperless): vários PDF de uma vez, password tirada em memória (com 2.ª password opcional),
verificação de texto, envio com o token do dono escolhido e nada gravado (nem a password). PDFs gerados aqui."""
import io

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from app import paperless, recibos_envio as re_
from app.main import create_app
from app.models import Auditoria
from tests.test_santander_pdf import pdf as pdf_texto
from tests.test_app import PW, _csrf, _post

TEXTO = ["Aviso de Credito / Debito", "Abonos", "Vencimento base 3.000,00", "Descontos", "IRS 900,00",
         "Acumulados", "Liquido 2.290,37"] * 3


def pdf_com_password(senha: str) -> bytes:
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_texto(TEXTO))))
    w.encrypt(senha, algorithm="AES-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_desproteger():
    limpo, protegido, paginas, texto = re_.desproteger(pdf_com_password("123456789"), ["errada", "123456789"])
    assert protegido and paginas == 1 and texto and not PdfReader(io.BytesIO(limpo)).is_encrypted
    with pytest.raises(ValueError, match="password errada"):
        re_.desproteger(pdf_com_password("123456789"), ["errada", ""])
    _, protegido, _, _ = re_.desproteger(pdf_texto(TEXTO), [""])
    assert not protegido


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-admin")
    enviados = []

    def enviar(nome, conteudo, *, token, titulo=None):
        enviados.append((nome, conteudo, token))
        return f"tarefa-{len(enviados)}"
    monkeypatch.setattr(paperless, "enviar_documento", enviar)
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    c.enviados, c.app_ = enviados, app
    return c


def test_varios_ficheiros(admin):
    t = admin.get("/importar/recibos")
    assert "Recibos de vencimento para o Paperless" in t.text and "Recibos (Paperless)" in t.text
    ficheiros = [("ficheiros", ("recibo_2025_12.pdf", pdf_com_password("123456789"), "application/pdf")),
                 ("ficheiros", ("recibo_2024_05.pdf", pdf_com_password("antiga99"), "application/pdf")),
                 ("ficheiros", ("outro.pdf", pdf_com_password("nenhuma"), "application/pdf")),
                 ("ficheiros", ("sem_password.pdf", pdf_texto(TEXTO), "application/pdf"))]
    # só verificar: nada vai para o Paperless
    r = admin.post("/importar/recibos", data={"csrf": _csrf(t), "senha": "123456789", "senha2": "antiga99",
                                              "acao": "verificar", "utilizador": "1"}, files=ficheiros)
    assert "verificado (não enviado)" in r.text and not admin.enviados
    r = admin.post("/importar/recibos", data={"csrf": _csrf(t), "senha": "123456789", "senha2": "antiga99",
                                              "acao": "enviar", "utilizador": "1"}, files=ficheiros)
    assert r.status_code == 200 and "3 enviados" in r.text and "password errada" in r.text
    nomes = [n for n, _, _ in admin.enviados]
    assert nomes == ["recibo_2025_12.pdf", "recibo_2024_05.pdf", "sem_password.pdf"]
    assert all(not PdfReader(io.BytesIO(c)).is_encrypted for _, c, _ in admin.enviados)
    assert all(tk == "token-admin" for _, _, tk in admin.enviados)
    # a password não fica em lado nenhum
    with admin.app_.state.SessionLocal() as db:
        detalhes = " ".join(a.detalhe or "" for a in db.scalars(select(Auditoria)))
    assert "123456789" not in detalhes and "antiga99" not in detalhes and "3 enviado(s)" in detalhes


def test_sem_ficheiros_ou_sem_token(admin, monkeypatch):
    t = admin.get("/importar/recibos")
    r = admin.post("/importar/recibos", data={"csrf": _csrf(t), "senha": "x", "acao": "enviar"})
    assert r.status_code == 400 and "Escolhe os ficheiros" in r.text
    monkeypatch.delenv("GESTOR_PAPERLESS_TOKEN")
    r = admin.post("/importar/recibos", data={"csrf": _csrf(t), "senha": "x", "acao": "enviar"},
                   files=[("ficheiros", ("a.pdf", pdf_texto(TEXTO), "application/pdf"))])
    assert "não tem token do Paperless" in r.text and not admin.enviados


def test_pedido_multipart_ao_paperless(monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    visto = {}

    class Resposta:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'"7a1b-tarefa"'

    def urlopen(pedido, timeout=0):
        visto["url"], visto["corpo"], visto["cab"] = pedido.full_url, pedido.data, dict(pedido.header_items())
        return Resposta()
    monkeypatch.setattr(paperless.urllib.request, "urlopen", urlopen)
    tarefa = paperless.enviar_documento('recibo "dez".pdf', b"%PDF-1.4 conteudo", token=" tok ")
    assert tarefa == "7a1b-tarefa" and visto["url"] == "http://paperless:8000/api/documents/post_document/"
    assert b'name="document"; filename="recibo dez.pdf"' in visto["corpo"] and b"%PDF-1.4 conteudo" in visto["corpo"]
    assert visto["cab"]["Authorization"] == "Token tok" and visto["cab"]["Content-type"].startswith("multipart/form-data; boundary=")
