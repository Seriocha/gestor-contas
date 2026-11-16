"""Garagem / Espaços dentro do Gestor: módulo por entidade, API (contrato da app autónoma), permissões, CSRF, desfazer,
duplicar, matrizes, pré-registo, exportar/importar (formato «garagem-export» v1) e o menu."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import garagem as g
from app.main import create_app
from app.models import Bem, Entidade, GarArtigo, GarLocal, Permissao, Utilizador
from app.seguranca import hash_password
from tests.test_app import PW, _post

A = "/garagem/app"
JPEG = b"\xff\xd8" + b"\x01" * 300


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    return c


def _modulo(app, ent=1, ligado=True):
    with app.state.SessionLocal() as db:
        e = db.get(Entidade, ent)
        lista = [m for m in e.modulos.split(",") if m and m != "garagem"]
        e.modulos = ",".join(lista + (["garagem"] if ligado else []))
        db.commit()


class Api:
    """Cliente da API da Garagem com o token anti-CSRF que a interface recebe no <meta name="csrf">."""

    def __init__(self, client):
        self.c = client
        self.token = re.search(r'name="csrf" content="([^"]+)"', client.get(f"{A}/").text).group(1)

    def _h(self, extra=None):
        return {"X-CSRF": self.token, **(extra or {})}

    def get(self, caminho, **kw):
        return self.c.get(f"{A}/api{caminho}", **kw)

    def post(self, caminho, corpo=None, **kw):
        return self.c.post(f"{A}/api{caminho}", json=corpo or {}, headers=self._h(), **kw)

    def put(self, caminho, corpo=None):
        return self.c.put(f"{A}/api{caminho}", json=corpo or {}, headers=self._h())

    def delete(self, caminho, corpo=None):
        return self.c.request("DELETE", f"{A}/api{caminho}", json=corpo, headers=self._h())

    def estado(self):
        return self.get("/state").json()

    def novo(self, caminho, corpo):
        r = self.post(caminho, corpo)
        assert r.status_code == 200, r.text
        return r.json()["id"]


@pytest.fixture
def api(admin, app):
    _modulo(app)
    return Api(admin)


def _utilizador(app, login, papel, ent=1):
    with app.state.SessionLocal() as db:
        u = Utilizador(nome=login.title(), login=login, password_hash=hash_password(PW), is_admin=False)
        db.add(u)
        db.flush()
        db.add(Permissao(utilizador_id=u.id, entidade_id=ent, papel=papel))
        db.commit()
    c = TestClient(app)
    _post(c, "/login", login=login, password=PW)
    return Api(c)


# --- módulo, página, menu e interface ---------------------------------------------------------------------------------
def test_sem_modulo_nada_abre(admin):
    assert admin.get("/garagem/").status_code == 404
    r = admin.get(f"{A}/api/state")
    assert r.status_code == 404 and "módulo Garagem" in r.json()["error"]
    assert "gg-espaco" not in admin.get("/inicio").text


def test_pagina_menu_e_interface(admin, app):
    _modulo(app)
    p = admin.get("/garagem/")
    assert p.status_code == 200 and 'id="gg-moldura"' in p.text
    inicio = admin.get("/inicio").text
    assert "gg-espaco" in inicio and 'href="/garagem/#/espaco/1"' in inicio and 'href="/garagem/#/inventario"' in inicio
    i = admin.get(f"{A}/")
    assert '<base href="/garagem/app/">' in i.text and 'name="csrf"' in i.text
    for ficheiro in ("app.js", "view3d.js", "menu-def.js", "style.css", "vendor/three/three.module.js", "vendor/three/three.core.js",
                     "vendor/three-addons/controls/OrbitControls.js", "vendor/jsQR.js"):
        assert admin.get(f"{A}/{ficheiro}").status_code == 200, ficheiro
    menu = admin.get(f"{A}/api/menu").json()
    assert menu["module"] == "garagem" and [gr["id"] for gr in menu["groups"]][:3] == ["espacos", "consulta", "registo"]
    assert menu["groups"][0]["items"][0]["href"] == "/garagem/#/espaco/1"


def test_csrf_obrigatorio_nas_alteracoes(api):
    r = api.c.post(f"{A}/api/locations", json={"name": "X"})
    assert r.status_code == 400 and "CSRF" in r.json()["error"]
    r = api.c.post(f"{A}/api/locations", json={"name": "X"}, headers={"X-CSRF": "errado"})
    assert r.status_code == 400
    assert api.post("/locations", {"name": "X"}).status_code == 200


def test_espaco_inicial_e_imovel(api, app):
    s = api.estado()
    assert [x["name"] for x in s["spaces"]] == ["Garagem"] and s["spaces"][0]["w"] == 6
    with app.state.SessionLocal() as db:
        db.add(Bem(entidade_id=1, tipo="casa", nome="Casa da Aldeia"))
        db.add(Bem(entidade_id=1, tipo="carro", nome="Carro"))
        db.commit()
        bem_id = db.scalar(select(Bem.id).where(Bem.nome == "Casa da Aldeia"))
    assert [b["nome"] for b in api.estado()["bens"]] == ["Casa da Aldeia"]            # só casas/rústicos
    eid = api.novo("/spaces", {"name": "Arrecadação", "w": 3, "d": 2.5, "h": 2.4, "property_id": str(bem_id), "property_name": "ignorado"})
    sp = next(x for x in api.estado()["spaces"] if x["id"] == eid)
    assert sp["property_id"] == str(bem_id) and sp["property_name"] == "Casa da Aldeia"
    assert [x["id"] for x in api.get("/spaces", params={"property_id": str(bem_id)}).json()] == [eid]
    assert api.put(f"/spaces/{eid}", {"name": "Arrecadação", "w": 3, "d": 2.5, "h": 2.4, "property_name": "Casa do avô"}).status_code == 200
    sp = next(x for x in api.estado()["spaces"] if x["id"] == eid)
    assert sp["property_id"] is None and sp["property_name"] == "Casa do avô"        # texto livre, sem Bem
    assert api.post("/spaces", {"name": "X", "w": 0, "d": 2, "h": 2}).status_code == 400
    assert api.post("/spaces", {"name": "", "w": 2, "d": 2, "h": 2}).status_code == 400


# --- fluxo completo ----------------------------------------------------------------------------------------------------
def test_fluxo_localizacoes_matriz_artigos(api):
    arm = api.novo("/locations", {"name": "Armário", "type": "armario", "plan_x": 0, "plan_y": 0, "plan_w": 1, "plan_h": 0.5, "plan_alt": 2})
    prat = api.novo("/locations", {"name": "Prateleira 1", "type": "prateleira", "parent_id": arm})
    caixa = api.novo("/locations", {"name": "Caixa", "type": "caixa", "parent_id": prat})
    assert api.post(f"/locations/{caixa}/grid", {"rows": 2, "cols": 3}).status_code == 200
    s = api.estado()
    celulas = [l for l in s["locations"] if l["parent_id"] == caixa]
    assert len(celulas) == 6 and {l["name"] for l in celulas} >= {"A1", "B3"} and all(l["grid_row"] is not None for l in celulas)
    root = next(l for l in s["locations"] if l["id"] == arm)
    assert root["space_id"] == s["spaces"][0]["id"] and len(root["code"]) == 5
    b2 = next(l["id"] for l in celulas if l["name"] == "B2")
    item = api.novo("/items", {"name": "Parafuso M4", "quantity": "200", "category": "Parafusos", "location_id": b2})
    # erros
    assert api.post("/items", {"name": ""}).status_code == 400
    assert api.put(f"/locations/{arm}", {"parent_id": caixa}).json()["error"].startswith("Não pode mover")
    assert api.delete(f"/locations/{prat}").status_code == 409
    assert api.post(f"/locations/{caixa}/grid", {"rows": 1, "cols": 1}).status_code == 409           # B2 tem um artigo
    # duplicar: numeração seguinte, com o que tem dentro
    ids = api.post(f"/locations/{prat}/duplicate", {"count": 2, "mode": "structure"}).json()["ids"]
    nomes = {l["id"]: l["name"] for l in api.estado()["locations"]}
    assert [nomes[i] for i in ids] == ["Prateleira 2", "Prateleira 3"]
    s = api.estado()
    assert sum(1 for l in s["locations"] if l["type"] == "caixa") == 3 and sum(1 for l in s["locations"] if l["grid_row"] is not None) == 18
    assert len(s["items"]) == 1                                                                         # artigos não copiados
    api.post(f"/locations/{prat}/duplicate", {"count": 1, "mode": "full", "name": "Prateleira 9"})
    assert len(api.estado()["items"]) == 2
    api.post(f"/locations/{prat}/duplicate", {"count": 1, "mode": "self"})
    assert sum(1 for l in api.estado()["locations"] if l["name"] == "Prateleira 4") == 1                # o 1.º número livre a seguir
    assert api.post(f"/locations/{prat}/duplicate", {"count": 99}).status_code == 400
    # zona do mapa: cópias lado a lado
    ids = api.post(f"/locations/{arm}/duplicate", {"count": 2, "mode": "self", "dx": 1.1}).json()["ids"]
    xs = {l["id"]: l["plan_x"] for l in api.estado()["locations"]}
    assert [xs[i] for i in ids] == [1.1, 2.2]
    # apagar a matriz passa os artigos para a caixa
    assert api.delete(f"/locations/{caixa}/grid", {"moveItems": True}).status_code == 200
    s = api.estado()
    assert next(i for i in s["items"] if i["id"] == item)["location_id"] == caixa
    assert not [l for l in s["locations"] if l["parent_id"] == caixa] and next(l for l in s["locations"] if l["id"] == caixa)["grid_rows"] is None
    # mover em lote e área temporária
    assert api.post("/items/move", {"ids": [item], "location_id": None}).json()["moved"] == 1
    assert next(i for i in api.estado()["items"] if i["id"] == item)["location_id"] is None
    assert api.post("/items/move", {"ids": [item], "location_id": 99999}).status_code == 400
    assert api.put(f"/items/{item}", {"location_id": prat}).status_code == 200
    assert api.delete(f"/items/{item}").status_code == 200


def test_desfazer_e_refazer(api):
    a = api.novo("/locations", {"name": "A"})
    api.novo("/locations", {"name": "B"})
    assert api.estado()["history"]["undo"] == 2 and api.estado()["history"]["undoLabel"] == "criar localização"
    assert api.post("/undo").json()["label"] == "criar localização"
    assert [l["name"] for l in api.estado()["locations"]] == ["A"] and api.estado()["history"]["redo"] == 1
    assert api.post("/redo").status_code == 200 and len(api.estado()["locations"]) == 2
    api.put(f"/locations/{a}", {"name": "A2"})
    assert api.estado()["history"]["redo"] == 0                                                           # nova alteração limpa o refazer
    api.post("/undo")
    assert next(l for l in api.estado()["locations"] if l["id"] == a)["name"] == "A"
    # apagar tudo também se desfaz
    assert api.post("/reset", {"confirm": "x"}).status_code == 400
    assert api.post("/reset", {"confirm": "APAGAR"}).status_code == 200
    assert api.estado()["locations"] == [] and len(api.estado()["spaces"]) == 1
    api.post("/undo")
    assert len(api.estado()["locations"]) == 2
    for _ in range(10):
        api.post("/undo")
    assert api.post("/undo").status_code == 409


def test_pre_registo_e_confirmacao(api):
    prat = api.novo("/locations", {"name": "Prateleira"})
    r = api.post("/drafts", {"rows": [{"name": "Martelo", "location_id": prat}, {"name": " Fita "}, {}, {"name": "X", "location_id": 9999}]})
    assert r.json()["added"] == 4
    s = api.estado()
    assert len(s["drafts"]) == 4 and s["drafts"][3]["location_id"] is None                              # localização desconhecida
    assert api.post("/drafts/confirm", {}).status_code == 400                                          # há uma linha sem nome
    vazia = next(d["id"] for d in s["drafts"] if not d["name"])
    assert api.put(f"/drafts/{next(d['id'] for d in s['drafts'] if d['name'] == 'Martelo')}", {"notes": "azul"}).status_code == 200
    assert api.delete(f"/drafts/{vazia}").status_code == 200
    assert api.put(f"/drafts/{vazia}", {"name": "x"}).status_code == 200                                # linha apagada: sem efeito
    r = api.post("/drafts/confirm", {}).json()
    assert r == {"created": 3, "temporary": 2}
    s = api.estado()
    assert s["drafts"] == [] and {i["name"] for i in s["items"]} == {"Martelo", "Fita", "X"}
    assert [i["location_id"] for i in s["items"] if i["name"] == "Martelo"] == [prat]
    assert next(i for i in s["items"] if i["name"] == "Martelo")["notes"] == "azul"
    api.post("/undo")
    assert len(api.estado()["drafts"]) == 3 and api.estado()["items"] == []                            # desfazer a confirmação devolve as linhas
    assert api.delete("/drafts").status_code == 200 and api.estado()["drafts"] == []


def test_fotos_etiquetas_e_qr(api, app, admin):
    assert api.c.post(f"{A}/api/photos", content=b"nao e jpeg", headers=api._h()).status_code == 400
    f = api.c.post(f"{A}/api/photos", content=JPEG, headers={**api._h(), "Content-Type": "image/jpeg"}).json()["file"]
    assert re.fullmatch(r"[a-f0-9]{24}\.jpg", f)
    assert api.c.get(f"{A}/photos/{f}").content == JPEG
    assert api.c.get(f"{A}/photos/{'0' * 24}.jpg").status_code == 404 and api.c.get(f"{A}/photos/..%2Fsecret.key").status_code in (404, 400)
    assert api.post("/items", {"name": "X", "photo": "../../x.jpg"}).status_code == 400
    loc = api.novo("/locations", {"name": "Caixa A", "photo": f})
    codigo = next(l["code"] for l in api.estado()["locations"] if l["id"] == loc)
    r = admin.get(f"/garagem/l/{codigo.lower()}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/garagem/#/loc/{loc}"
    assert admin.get(f"{A}/l/ZZZZZ", follow_redirects=False).headers["location"] == "/garagem/"
    qr = api.get("/qr.svg", params={"text": "http://x/l/" + codigo})
    assert qr.status_code == 200 and qr.headers["content-type"].startswith("image/svg+xml") and "<svg" in qr.text


# --- permissões e isolamento entre entidades --------------------------------------------------------------------------
def test_permissoes_por_papel(api, app):
    api.novo("/locations", {"name": "Armário"})
    ver = _utilizador(app, "ana", "ver")
    assert [l["name"] for l in ver.estado()["locations"]] == ["Armário"]
    assert ver.post("/locations", {"name": "X"}).status_code == 403
    assert ver.post("/undo").status_code == 403
    assert ver.get("/export").status_code == 200                                                         # ler/exportar basta «ver»
    editor = _utilizador(app, "rui", "editar")
    assert editor.post("/locations", {"name": "Prateleira"}).status_code == 200
    assert editor.post("/reset", {"confirm": "APAGAR"}).status_code == 403
    bundle = editor.get("/export").json()
    assert editor.post("/import?mode=replace", bundle).status_code == 403                                # substituir só administradores
    assert editor.post("/import?mode=merge", bundle).status_code == 200
    adm = _utilizador(app, "eva", "admin")
    assert adm.post("/import?mode=replace", bundle).status_code == 200


def test_entidades_isoladas(api, app):
    _modulo(app, 2)
    api.novo("/locations", {"name": "Só da Família"})
    assert api.c.get("/garagem/entidade/2", follow_redirects=False).status_code == 303
    outra = Api(api.c)
    assert outra.estado()["locations"] == [] and len(outra.estado()["spaces"]) == 1                      # a Mãe tem o seu espaço inicial
    outra.novo("/locations", {"name": "Só da Mãe"})
    api.c.get("/garagem/entidade/1")
    assert [l["name"] for l in Api(api.c).estado()["locations"]] == ["Só da Família"]
    # uma entidade não mexe nas localizações de outra
    with app.state.SessionLocal() as db:
        da_mae = db.scalar(select(GarLocal.id).where(GarLocal.name == "Só da Mãe"))
    r = Api(api.c).put(f"/locations/{da_mae}", {"name": "roubada"})
    assert r.status_code == 404


# --- exportar / importar ----------------------------------------------------------------------------------------------
def _montar(api):
    esp2 = api.novo("/spaces", {"name": "Arrecadação", "w": 3, "d": 2.5, "h": 2.4, "property_name": "Casa da Aldeia", "property_id": "77"})
    f = api.c.post(f"{A}/api/photos", content=JPEG, headers={**api._h(), "Content-Type": "image/jpeg"}).json()["file"]
    arm = api.novo("/locations", {"name": "Armário", "type": "armario", "photo": f, "plan_x": 0.5, "plan_y": 0, "plan_w": 1, "plan_h": 0.5,
                                  "plan_wall": "fundo", "space_id": esp2})
    caixa = api.novo("/locations", {"name": "Caixa", "type": "caixa", "parent_id": arm})
    api.post(f"/locations/{caixa}/grid", {"rows": 2, "cols": 2})
    a1 = next(l["id"] for l in api.estado()["locations"] if l["parent_id"] == caixa and l["name"] == "A1")
    api.novo("/items", {"name": "Parafuso", "quantity": "10", "photo": f, "location_id": a1})
    api.novo("/items", {"name": "Fita"})
    api.post("/drafts", {"rows": [{"name": "Rascunho", "location_id": caixa}]})
    return f


def test_exportar_e_importar_a_substituir(api):
    f = _montar(api)
    antes = api.estado()
    r = api.get("/export")
    assert "attachment" in r.headers["content-disposition"]
    b = r.json()
    assert b["format"] == "garagem-export" and b["version"] == 1 and b["source"] == "gestor"
    assert len(b["spaces"]) == 2 and len(b["locations"]) == 6 and len(b["items"]) == 2 and len(b["drafts"]) == 1
    assert list(b["photos"]) == [f] and api.get("/export", params={"photos": "0"}).json()["photos"] == {}
    res = api.post("/import?mode=replace", b).json()
    assert res["locations"] == 6 and res["items"] == 2 and res["drafts"] == 1 and res["codigos_novos"] == 0
    depois = api.estado()
    assert {l["code"] for l in depois["locations"]} == {l["code"] for l in antes["locations"]}             # etiquetas continuam válidas
    assert sorted(i["name"] for i in depois["items"]) == ["Fita", "Parafuso"]
    assert {i["name"]: pathname(depois, i) for i in depois["items"]}["Parafuso"] == "Armário > Caixa > A1"
    sp = next(s for s in depois["spaces"] if s["name"] == "Arrecadação")
    assert sp["property_name"] == "Casa da Aldeia" and sp["property_id"] == "77"                          # sem Bem: referência mantém-se
    root = next(l for l in depois["locations"] if l["name"] == "Armário")
    assert root["space_id"] == sp["id"] and root["plan_wall"] == "fundo" and root["photo"] == f
    api.post("/undo")                                                                                       # a importação também se desfaz
    assert {l["code"] for l in api.estado()["locations"]} == {l["code"] for l in antes["locations"]}


def pathname(estado, item):
    por_id = {l["id"]: l for l in estado["locations"]}
    nomes, l = [], por_id.get(item["location_id"])
    while l:
        nomes.insert(0, l["name"])
        l = por_id.get(l["parent_id"])
    return " > ".join(nomes)


def test_importar_acrescentar_noutra_entidade_liga_imoveis_pelo_nome(api, app):
    _montar(api)
    bundle = api.get("/export").json()
    _modulo(app, 2)
    with app.state.SessionLocal() as db:
        db.add(Bem(entidade_id=2, tipo="casa", nome="casa da aldeia"))                                      # nome igual (sem distinguir maiúsculas)
        db.commit()
        bem2 = db.scalar(select(Bem.id).where(Bem.entidade_id == 2))
    api.c.get("/garagem/entidade/2")
    outra = Api(api.c)
    res = outra.post("/import?mode=merge", bundle).json()
    assert res["spaces"] == 2 and res["locations"] == 6 and res["items"] == 2 and res["espacos_sem_imovel"] == []
    assert res["codigos_novos"] == 6                                                                         # os códigos já existem na entidade 1
    s = outra.estado()
    assert [x["name"] for x in s["spaces"]].count("Arrecadação") == 1 and len(s["spaces"]) == 2            # o espaço inicial vazio deu lugar aos do ficheiro
    assert next(x for x in s["spaces"] if x["name"] == "Arrecadação")["property_id"] == str(bem2)
    assert len(s["items"]) == 2 and (outra.c.get(f"{A}/photos/{next(i for i in s['items'] if i['photo'])['photo']}").content == JPEG)
    assert outra.post("/import?mode=merge", bundle).json()["locations"] == 6                              # voltar a acrescentar duplica
    assert len(outra.estado()["items"]) == 4


def test_importar_ficheiro_da_app_autonoma(api, tmp_path):
    """Ficheiro como o gera a app autónoma (server.js): ids não contíguos, pai depois do filho, fotos maliciosas ignoradas."""
    b = {"format": "garagem-export", "version": 1, "exported": "2026-11-16T10:00:00Z", "source": "standalone",
         "spaces": [{"id": 5, "name": "Garagem velha", "w": 6, "d": 5, "h": 3, "property_id": "7", "property_name": "Casa X"}],
         "locations": [
             {"id": 30, "parent_id": 20, "space_id": None, "name": "Caixa", "type": "caixa", "code": "ABCDE", "grid_rows": None},
             {"id": 20, "parent_id": None, "space_id": 5, "name": "Estante", "type": "estante", "code": "ZZZZZ", "plan_x": 1, "plan_y": 1,
              "plan_w": 2, "plan_h": 0.5, "plan_z": None, "plan_alt": 2, "plan_wall": "teto", "plan_face": "cima", "photo": "../../x.jpg"}],
         "items": [{"id": 1, "name": "Cola", "location_id": 30, "updated": "2026-11-01 10:00:00", "photo": "../../y.jpg"},
                   {"id": 2, "name": "", "location_id": None}, {"id": 3, "name": "Solto", "location_id": 999}],
         "drafts": [{"id": 1, "name": "R1", "location_id": 20}],
         "photos": {"../evil.jpg": "AAAA", "aaaaaaaaaaaaaaaaaaaaaaaa.jpg": "bm9wZQ=="}}
    res = api.post("/import?mode=merge", b).json()
    assert (res["spaces"], res["locations"], res["items"], res["drafts"]) == (1, 2, 2, 1)
    assert res["espacos_sem_imovel"] == ["Garagem velha"]
    s = api.estado()
    est = next(l for l in s["locations"] if l["name"] == "Estante")
    assert est["code"] == "ZZZZZ" and est["plan_wall"] is None and est["plan_face"] is None and est["photo"] is None
    cola = next(i for i in s["items"] if i["name"] == "Cola")
    assert cola["updated"] == "2026-11-01 10:00:00" and cola["photo"] is None and pathname(s, cola) == "Estante > Caixa"
    assert next(i for i in s["items"] if i["name"] == "Solto")["location_id"] is None
    assert api.c.get(f"{A}/photos/aaaaaaaaaaaaaaaaaaaaaaaa.jpg").status_code == 404                        # não era um JPEG
    # inválidos
    assert api.post("/import", {"x": 1}).status_code == 400
    ciclo = {**b, "locations": [{"id": 1, "parent_id": 2, "name": "A"}, {"id": 2, "parent_id": 1, "name": "B"}], "items": [], "drafts": []}
    r = api.post("/import", ciclo)
    assert r.status_code == 400 and "Hierarquia" in r.json()["error"]
    assert len(api.estado()["locations"]) == 2                                                             # falhou sem deixar nada a meio


# --- menu em dados -----------------------------------------------------------------------------------------------------
def test_construir_menu_hierarquia_e_filtros():
    esp = [{"id": 1, "name": "Garagem", "w": 6, "d": 5, "h": 3, "property_id": "7", "property_name": "Casa"},
           {"id": 2, "name": "Arrecadação", "w": 3, "d": 2, "h": 2.4, "property_id": None, "property_name": None}]
    loc = [{"id": 1, "parent_id": None, "name": "Mezanine", "type": "mezanine", "grid_row": None, "space_id": 1},
           {"id": 2, "parent_id": 1, "name": "Estante 10", "type": "estante", "grid_row": None, "space_id": None},
           {"id": 3, "parent_id": 1, "name": "Estante 2", "type": "estante", "grid_row": None, "space_id": None},
           {"id": 4, "parent_id": 2, "name": "A1", "type": "gaveta", "grid_row": 0, "space_id": None},
           {"id": 5, "parent_id": None, "name": "Armário", "type": "armario", "grid_row": None, "space_id": 2}]
    m = g.construir_menu(esp, loc, n_temp=3, n_rascunhos=0, prefixo="/garagem/", espaco_atual=1)
    assert [gr["id"] for gr in m["groups"]] == ["espacos", "consulta", "registo", "mapas", "dados"]      # espaços antes de Procurar
    garagem = m["groups"][0]["items"][0]
    assert garagem["href"] == "/garagem/#/espaco/1" and garagem["current"] and garagem["property"] == {"id": "7", "name": "Casa"}
    mez = garagem["children"][0]
    assert mez["title"] == "Mezanine" and [c["title"] for c in mez["children"]] == ["Estante 2", "Estante 10"]    # ordem natural
    assert m["groups"][0]["items"][1]["children"][0]["title"] == "Armário" and m["groups"][0]["items"][-1]["action"] == "new-space"
    assert m["groups"][2]["items"][1]["badge"] == 3 and m["groups"][2]["items"][0]["badge"] is None
    so_imovel = g.construir_menu(esp, loc, 0, 0, imovel="7")
    assert [i["title"] for i in so_imovel["groups"][0]["items"]] == ["Garagem", "Novo espaço"]


# --- sobreposição no mapa ----------------------------------------------------------------------------------------------
def test_zonas_ao_mesmo_nivel_nao_se_sobrepoem(api):
    a = api.novo("/locations", {"name": "A", "type": "estante", "plan_x": 0, "plan_y": 0, "plan_w": 1, "plan_h": 1, "plan_alt": 2})
    b = api.novo("/locations", {"name": "B", "type": "estante", "plan_x": 2, "plan_y": 0, "plan_w": 1, "plan_h": 1, "plan_alt": 2})
    r = api.put(f"/locations/{b}", {"plan_x": 0.5})
    assert r.status_code == 409 and "A" in r.json()["error"]
    assert api.put(f"/locations/{b}", {"plan_x": 1}).status_code == 200          # encostada (sem área comum) é permitido
    # por cima da outra (outro nível) é permitido; ao nível dela não
    assert api.put(f"/locations/{b}", {"plan_x": 0, "plan_z": 2}).status_code == 200
    assert api.put(f"/locations/{b}", {"plan_z": 1.5}).status_code == 409
    # girar uma zona também conta (a área no chão muda)
    c = api.novo("/locations", {"name": "C", "type": "estante", "plan_x": 3, "plan_y": 0, "plan_w": 2, "plan_h": 0.5, "plan_alt": 2})
    assert api.put(f"/locations/{c}", {"plan_wall": "esquerda", "plan_x": 0}).status_code == 409


def test_interface_leva_a_versao_nos_ficheiros(api):
    html = api.c.get(f"{A}/").text
    assert re.search(r'src="app\.js\?v=\d+\.\d+\.\d+"', html) and re.search(r'href="style\.css\?v=\d+\.\d+\.\d+"', html)
