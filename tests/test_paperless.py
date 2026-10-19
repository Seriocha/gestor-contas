"""Paperless-ngx: procurar documentos (valor, comerciante, texto), ligar/desligar a movimentos e itens do recheio,
miniaturas pelo Gestor. A API do Paperless é simulada (nada sai da máquina)."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import paperless
from app.main import create_app
from app.models import DocumentoLigado, ItemRecheio, Movimento
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov

D = date.today() - timedelta(days=5)


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _mov(c, 1, D.isoformat(), "84,91", "COMPRA 1234 WORTEN AMADORA", sentido="saida")
    return c


@pytest.fixture
def api(monkeypatch):
    """Paperless simulado: regista os pedidos e responde como a API REST."""
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-de-teste")
    monkeypatch.setenv("GESTOR_PAPERLESS_LINK", "http://nas:8300")
    pedidos = []
    docs = {7: {"id": 7, "title": "Fatura Worten FT 2026/123", "created": D.isoformat(), "correspondent": 3,
                "document_type": 1, "original_file_name": "fatura.pdf"},
            8: {"id": 8, "title": "Garantia máquina", "created": (D - timedelta(days=2)).isoformat(), "correspondent": 3,
                "document_type": 2, "original_file_name": "garantia.pdf"}}

    def falso(caminho, params=None, binario=False, token=None):
        if not token:
            raise paperless.ErroPaperless("Guarda o teu token do Paperless em Perfil.")
        if token == "token-errado":
            raise paperless.ErroPaperless("O Paperless respondeu 401. Confirma o token.")
        pedidos.append((caminho, dict(params or {}), token))
        if binario:
            return b"RIFF\x00\x00\x00\x00WEBPVP8 "
        if caminho == "/api/correspondents/":
            return {"results": [{"id": 3, "name": "Worten"}]}
        if caminho == "/api/document_types/":
            return {"results": [{"id": 1, "name": "Fatura"}, {"id": 2, "name": "Garantia"}]}
        if caminho.startswith("/api/documents/") and caminho.rstrip("/").split("/")[-1].isdigit():
            return docs[int(caminho.rstrip("/").split("/")[-1])]
        if caminho == "/api/documents/" and (params or {}).get("page_size") == 1:
            return {"count": 2, "results": []}
        q = (params or {}).get("query") or ""
        achados = [docs[7]] if "84,91" in q else [docs[7], docs[8]] if "Worten" in q else []
        return {"results": achados}
    monkeypatch.setattr(paperless, "_pedido", falso)
    return pedidos


def test_sem_paperless_configurado(admin, app, monkeypatch):
    monkeypatch.delenv("GESTOR_PAPERLESS_URL", raising=False)
    monkeypatch.delenv("GESTOR_PAPERLESS_TOKEN", raising=False)
    p = admin.get("/movimentos/1/detalhe").text
    assert "📎 Documentos" in p and "Liga o Gestor ao Paperless-ngx" in p and "Procurar no Paperless" not in p
    assert 'id="paperless"' not in admin.get("/perfil").text


def test_procurar_ligar_e_desligar_no_movimento(admin, app, api):
    p = admin.get("/movimentos/1/detalhe").text
    assert "Procurar no Paperless" in p and 'data-url="/documentos/procurar?alvo=movimento&alvo_id=1"' in p
    r = admin.get("/documentos/procurar?alvo=movimento&alvo_id=1")
    assert "<!doctype" not in r.text.lower() and "Fatura Worten FT 2026/123" in r.text and "Garantia máquina" in r.text
    assert "encontrado pelo valor 84,91 €" in r.text and "http://nas:8300/documents/7/details" in r.text
    consultas = [q for c, q, t in api if c == "/api/documents/"]
    assert all(t == "token-de-teste" for _, _, t in api)          # administrador sem token pessoal: o partilhado
    assert consultas[0]["query"] == '"84,91"' and consultas[1]["query"] == "Worten"
    assert consultas[0]["created__date__gt"] == (D - timedelta(days=31)).isoformat()
    admin.post("/documentos/ligar", data={"csrf": _csrf(admin.get("/movimentos/1/detalhe")), "alvo": "movimento",
                                          "alvo_id": "1", "paperless_id": "7"})
    with app.state.SessionLocal() as db:
        d = db.scalar(select(DocumentoLigado))
        assert (d.paperless_id, d.titulo, d.correspondente, d.data, d.movimento_id) == (7, "Fatura Worten FT 2026/123", "Worten", D, 1)
        did = d.id
    p = admin.get("/movimentos/1/detalhe").text
    assert "/paperless/7/miniatura" in p and "Desligar" in p
    assert '<span class="mapa-no-rotulo">Documento</span>' in p                       # nó no mapa de ligações
    assert "ligado</span>" in admin.get("/documentos/procurar?alvo=movimento&alvo_id=1").text
    m = admin.get("/paperless/7/miniatura")
    assert m.status_code == 200 and m.headers["content-type"] == "image/webp"
    admin.post(f"/documentos/{did}/desligar", data={"csrf": _csrf(admin.get("/movimentos/1/detalhe"))})
    with app.state.SessionLocal() as db:
        assert db.scalar(select(DocumentoLigado)) is None


def test_documentos_no_item_do_recheio(admin, app, api):
    with app.state.SessionLocal() as db:
        it = ItemRecheio(entidade_id=1, nome="Máquina de lavar", loja="Worten", data_compra=D, movimento_id=1)
        db.add(it)
        db.commit()
        iid = it.id
    p = admin.get(f"/patrimonio/recheio/{iid}").text
    assert "📎 Documentos" in p and f"alvo=recheio&alvo_id={iid}" in p
    r = admin.get(f"/documentos/procurar?alvo=recheio&alvo_id={iid}").text
    assert "Garantia máquina" in r
    admin.post("/documentos/ligar", data={"csrf": _csrf(admin.get(f"/patrimonio/recheio/{iid}")), "alvo": "recheio",
                                          "alvo_id": str(iid), "paperless_id": "8"})
    with app.state.SessionLocal() as db:
        assert db.scalar(select(DocumentoLigado)).item_recheio_id == iid
    assert admin.get("/documentos/procurar?alvo=xpto&alvo_id=1").status_code == 404


def test_pedido_sem_versao_fixa_da_api(monkeypatch):
    """O pedido não fixa a versão da API (com «version=5» o Paperless atual responde 406) e leva o token."""
    import io
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000/")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "abc123 ")
    vistos = []

    class Resposta(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(pedido, timeout=0):
        vistos.append(pedido)
        return Resposta(b'{"results": []}')
    monkeypatch.setattr(paperless.urllib.request, "urlopen", urlopen)
    assert paperless._pedido("/api/documents/", {"query": "worten", "vazio": ""}, token="abc123 ") == {"results": []}
    p = vistos[0]
    assert p.full_url == "http://paperless:8000/api/documents/?query=worten"
    assert p.get_header("Accept") == "application/json" and p.get_header("Authorization") == "Token abc123"
    paperless._pedido("/api/documents/7/thumb/", binario=True, token="abc123")
    assert vistos[1].get_header("Accept") == "*/*"



def test_token_pessoal_por_utilizador(admin, app, api):
    """Cada utilizador usa o seu token (cifrado); sem token, um utilizador normal não vê o Paperless."""
    from app.models import Permissao, Utilizador
    from app.seguranca import hash_password
    with app.state.SessionLocal() as db:
        db.add(Utilizador(nome="Ana", login="ana", password_hash=hash_password(PW), is_admin=False))
        db.flush()
        db.add(Permissao(utilizador_id=db.scalar(select(Utilizador.id).where(Utilizador.login == "ana")), entidade_id=1, papel="editar"))
        db.commit()
    ana = TestClient(app)
    _post(ana, "/login", login="ana", password=PW)
    p = ana.get("/movimentos/1/detalhe").text
    assert "guarda o teu token do Paperless em" in p and "Procurar no Paperless" not in p        # não usa o partilhado
    assert "sem token" in ana.get("/perfil").text
    assert "Guarda o teu token" in ana.get("/documentos/procurar?alvo=movimento&alvo_id=1").text
    # token recusado não é guardado
    r = ana.post("/perfil/paperless", data={"csrf": _csrf(ana.get("/perfil")), "acao": "guardar", "token": "token-errado"})
    assert "Token recusado pelo Paperless" in r.text and "sem token" in r.text
    r = ana.post("/perfil/paperless", data={"csrf": _csrf(ana.get("/perfil")), "acao": "guardar", "token": " token-da-ana "})
    assert "Token do Paperless guardado. Ligação ao Paperless confirmada: vês 2 documentos." in r.text
    assert "token pessoal guardado" in r.text
    with app.state.SessionLocal() as db:
        cif = db.scalar(select(Utilizador).where(Utilizador.login == "ana")).paperless_token
        assert cif and "token-da-ana" not in cif                                   # cifrado
        assert paperless.decifrar(cif, app.state.defs.secret_key) == "token-da-ana"
    api.clear()
    assert "Fatura Worten" in ana.get("/documentos/procurar?alvo=movimento&alvo_id=1").text
    assert {t for _, _, t in api} == {"token-da-ana"}
    assert ana.get("/paperless/7/miniatura").status_code == 200
    assert "Ligação ao Paperless confirmada" in ana.post("/perfil/paperless", data={"csrf": _csrf(ana.get("/perfil")), "acao": "testar"}).text
    ana.post("/perfil/paperless", data={"csrf": _csrf(ana.get("/perfil")), "acao": "remover"})
    assert "sem token" in ana.get("/perfil").text
    # o administrador sem token pessoal continua com o partilhado
    assert "a usar o token partilhado" in admin.get("/perfil").text
