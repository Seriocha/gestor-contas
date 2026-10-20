"""Immich: chave pessoal em Perfil, fotos da viagem (datas pela hora local, lugares do EXIF), miniaturas pelo Gestor,
álbum da viagem no Immich e destino a partir das fotos. A API do Immich é simulada (nada sai da máquina)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import immich
from app.main import create_app
from app.models import Utilizador, Viagem
from tests.test_app import PW, _csrf, _post

A1, A2, A3, FORA = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
                    "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("GESTOR_IMMICH_URL", "http://immich_server:2283")
    monkeypatch.setenv("GESTOR_IMMICH_LINK", "http://nas:2283")
    monkeypatch.delenv("GESTOR_IMMICH_TOKEN", raising=False)
    estado = {"pedidos": [], "albuns": {}}

    def falso(caminho, metodo="GET", corpo=None, binario=False, chave=None):
        if not chave:
            raise immich.ErroImmich("Guarda a tua chave da API do Immich em Perfil.")
        if chave == "chave-errada":
            raise immich.ErroImmich("O Immich respondeu 401. Confirma a chave da API.")
        estado["pedidos"].append((metodo, caminho, corpo, chave))
        if binario:
            return b"RIFF\x00\x00\x00\x00WEBPVP8 "
        if caminho == "/api/users/me":
            return {"name": "Sérgio"}
        if caminho == "/api/search/metadata":
            return {"assets": {"nextPage": None, "items": [
                {"id": A1, "type": "IMAGE", "originalFileName": "praia.jpg", "localDateTime": "2025-08-10T11:00:00.000Z",
                 "exifInfo": {"city": "Albufeira", "country": "Portugal"}},
                {"id": A2, "type": "IMAGE", "originalFileName": "jantar.jpg", "localDateTime": "2025-08-12T21:30:00.000Z",
                 "exifInfo": {"city": "Albufeira", "country": "Portugal"}},
                {"id": A3, "type": "VIDEO", "originalFileName": "barco.mp4", "localDateTime": "2025-08-14T16:00:00.000Z",
                 "exifInfo": {"city": "Lagos", "country": "Portugal"}},
                # devolvida pela margem de 12 h da pesquisa, mas tirada (hora local) no dia seguinte ao regresso
                {"id": FORA, "type": "IMAGE", "originalFileName": "casa.jpg", "localDateTime": "2025-08-15T09:00:00.000Z",
                 "exifInfo": {"city": "Lisboa", "country": "Portugal"}}]}}
        if caminho == "/api/albums" and metodo == "POST":
            estado["albuns"]["alb-1"] = list(corpo["assetIds"])
            return {"id": "alb-1", "albumName": corpo["albumName"]}
        if caminho.startswith("/api/albums/alb-1/assets"):
            novos = [i for i in corpo["ids"] if i not in estado["albuns"]["alb-1"]]
            estado["albuns"]["alb-1"] += novos
            return [{"id": i, "success": i in novos} for i in corpo["ids"]]
        if caminho.startswith("/api/albums/"):
            aid = caminho.split("/")[3].split("?")[0]
            if aid in estado["albuns"]:
                return {"id": aid, "assetCount": len(estado["albuns"][aid])}
            raise immich.ErroImmich("O Immich respondeu 404.")
        raise AssertionError(caminho)
    monkeypatch.setattr(immich, "_pedido", falso)
    return estado


@pytest.fixture
def app(tmp_path, api):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    f = c.get("/viagens/nova?entidade=1")
    c.post("/viagens/nova?entidade=1", data={"csrf": _csrf(f), "nome": "Algarve 2025", "data_ini": "2025-08-10",
                                             "data_fim": "2025-08-14", "pessoas": "2", "motivo": "lazer"})
    return c


def _guardar_chave(c, chave="chave-boa"):
    return c.post("/perfil/immich", data={"csrf": _csrf(c.get("/perfil")), "acao": "guardar", "token": chave})


def test_chave_pessoal_no_perfil(admin, app):
    p = admin.get("/perfil").text
    assert 'id="immich"' in p and "sem chave" in p
    r = _guardar_chave(admin, "chave-errada")
    assert "recusada" in r.text
    r = _guardar_chave(admin)
    assert "Chave do Immich guardada. Ligação ao Immich confirmada: Sérgio." in r.text
    with app.state.SessionLocal() as db:
        u = db.scalar(select(Utilizador))
        assert u.immich_token and "chave-boa" not in u.immich_token          # cifrada
    r = admin.post("/perfil/immich", data={"csrf": _csrf(admin.get("/perfil")), "acao": "remover"})
    assert "Chave do Immich removida." in r.text


def test_fotos_da_viagem_e_album(admin, app, api):
    t = admin.get("/viagens/1").text
    assert 'id="fotos" data-url="/viagens/1/fotos"' in t
    assert "Perfil</a> para veres aqui as fotos" in admin.get("/viagens/1/fotos").text      # ainda sem chave
    _guardar_chave(admin)
    f = admin.get("/viagens/1/fotos").text
    assert "3 no Immich" in f and A1 in f and A3 in f and FORA not in f                   # só a hora local conta
    assert "Albufeira, Portugal" in f and "Lagos, Portugal" in f and "▶" in f
    assert f'href="http://nas:2283/photos/{A1}"' in f
    # pesquisa com 12 h de margem para os fusos
    _, _, corpo, _ = next(p for p in api["pedidos"] if p[1] == "/api/search/metadata")
    assert corpo["takenAfter"] == "2025-08-09T12:00:00.000Z" and corpo["takenBefore"].startswith("2025-08-15T11:59:59")
    # miniatura pelo Gestor
    m = admin.get(f"/viagens/fotos/{A1}/miniatura")
    assert m.status_code == 200 and m.headers["content-type"] == "image/webp"
    assert admin.get("/viagens/fotos/nao-e-um-id/miniatura").status_code == 404
    # destino a partir das fotos
    admin.post("/viagens/1/destino-das-fotos", data={"csrf": _csrf(admin.get("/viagens/1")), "cidade": "Albufeira", "pais": "Portugal"})
    # criar o álbum e, depois, juntar só o que falta
    r = admin.post("/viagens/1/album", data={"csrf": _csrf(admin.get("/viagens/1"))})
    assert "Álbum «Algarve 2025» criado no Immich com 3 fotos." in r.text
    assert api["albuns"]["alb-1"] == [A1, A2, A3]
    with app.state.SessionLocal() as db:
        v = db.get(Viagem, 1)
        assert (v.immich_album_id, v.destino, v.pais) == ("alb-1", "Albufeira", "Portugal")
    f = admin.get("/viagens/1/fotos").text
    assert "Abrir o álbum (3)" in f and "http://nas:2283/albums/alb-1" in f and "Juntar ao álbum" in f
    r = admin.post("/viagens/1/album", data={"csrf": _csrf(admin.get("/viagens/1"))})
    assert "O álbum já tinha todas as fotos." in r.text
    r = admin.post("/viagens/1/album/desligar", data={"csrf": _csrf(admin.get("/viagens/1"))})
    assert "continua no Immich" in r.text


def test_sem_immich_configurado(admin, monkeypatch):
    monkeypatch.delenv("GESTOR_IMMICH_URL")
    assert 'id="fotos"' not in admin.get("/viagens/1").text
    assert 'id="immich"' not in admin.get("/perfil").text


def test_menu_viagens_agrupamento(admin):
    p = admin.get("/inicio").text
    assert '<p class="menu-grupo">Viagens</p>' in p and 'href="/viagens/detetar"' in p
    assert p.index('<p class="menu-grupo">Património</p>') < p.index('<p class="menu-grupo">Viagens</p>') < p.index('<p class="menu-grupo">Dados</p>')
