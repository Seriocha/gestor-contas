"""Aviso de ligação sem HTTPS (ou sem cookie seguro) na Administração."""
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import create_app
from app.seguranca import aviso_ligacao
from tests.test_app import PW, _post


def _pedido(url: str, cookie_secure: bool = False) -> Request:
    esquema, resto = url.split("://")
    host = resto.split("/")[0]
    app = SimpleNamespace(state=SimpleNamespace(defs=SimpleNamespace(cookie_secure=cookie_secure)))
    return Request({"type": "http", "scheme": esquema, "server": (host, 443 if esquema == "https" else 80), "path": "/admin/backups",
                    "headers": [(b"host", host.encode())], "query_string": b"", "app": app})


def test_aviso_ligacao():
    assert "não é cifrada (HTTP)" in aviso_ligacao(_pedido("http://192.168.1.232:8200"))
    assert aviso_ligacao(_pedido("http://localhost:8200")) is None and aviso_ligacao(_pedido("http://127.0.0.1:8201")) is None
    assert "GESTOR_COOKIE_SECURE=1" in aviso_ligacao(_pedido("https://gestor.nas.lan"))
    assert aviso_ligacao(_pedido("https://gestor.nas.lan", cookie_secure=True)) is None


def test_aviso_na_administracao(tmp_path):
    c = TestClient(create_app(tmp_path, backups_automaticos=False), base_url="http://192.168.1.232:8200")
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    assert "não é cifrada (HTTP)" in c.get("/admin/backups").text
    assert "não é cifrada" not in c.get("/inicio").text                  # só nas páginas da Administração
