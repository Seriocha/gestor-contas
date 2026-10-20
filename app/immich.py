"""Ligação ao Immich da NAS (fotografias): as fotos tiradas durante uma viagem e o álbum da viagem no Immich.

O Immich guarda as fotos; o Gestor só as procura pela API (data em que foram tiradas, cidade/país do EXIF), mostra as
miniaturas (pelo Gestor, com a chave de quem está a ver) e pode criar no Immich o álbum da viagem. Configuração no
ficheiro immich.env da NAS:
  GESTOR_IMMICH_URL    endereço visto pelo container do Gestor (ex.: http://immich_server:2283, na rede «nas-apps»)
  GESTOR_IMMICH_LINK   endereço para o browser abrir as fotos (ex.: http://192.168.1.232:2283)
  GESTOR_IMMICH_TOKEN  (opcional) chave partilhada, usada só por administradores sem chave pessoal

Multiutilizador, como no Paperless: cada pessoa guarda em Perfil a SUA chave da API do Immich (cifrada na base de
dados); cada um vê só as fotos da sua biblioteca no Immich.
"""
import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


class ErroImmich(RuntimeError):
    pass


def url_api() -> str:
    return (os.environ.get("GESTOR_IMMICH_URL") or "").rstrip("/")


def url_browser() -> str:
    return (os.environ.get("GESTOR_IMMICH_LINK") or url_api()).rstrip("/")


def configurado() -> bool:
    return bool(url_api())


# ------------------------------------------------------------------ chave pessoal (cifrada)
def _fernet(segredo: str):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"gestor-immich|" + segredo.encode()).digest()))


def cifrar(chave: str, segredo: str) -> str:
    return _fernet(segredo).encrypt(chave.strip().encode()).decode()


def decifrar(cifrado: str | None, segredo: str) -> str | None:
    if not cifrado:
        return None
    try:
        return _fernet(segredo).decrypt(cifrado.encode()).decode()
    except Exception:        # a chave secreta da app mudou: a chave do Immich tem de ser guardada outra vez
        return None


def chave_de(u, segredo: str) -> str | None:
    """A chave com que este utilizador fala com o Immich: a pessoal; senão, para administradores, a partilhada."""
    if not configurado() or u is None:
        return None
    pessoal = decifrar(getattr(u, "immich_token", None), segredo)
    if pessoal:
        return pessoal
    partilhada = (os.environ.get("GESTOR_IMMICH_TOKEN") or "").strip()
    return partilhada if partilhada and u.is_admin else None


def _pedido(caminho: str, metodo: str = "GET", corpo: dict | None = None, binario: bool = False, chave: str | None = None):
    if not configurado():
        raise ErroImmich("O Immich não está configurado (immich.env na NAS).")
    if not chave:
        raise ErroImmich("Guarda a tua chave da API do Immich em Perfil.")
    dados = json.dumps(corpo).encode() if corpo is not None else None
    cabecalhos = {"x-api-key": chave.strip(), "Accept": "*/*" if binario else "application/json"}
    if dados is not None:
        cabecalhos["Content-Type"] = "application/json"
    pedido = urllib.request.Request(f"{url_api()}{caminho}", data=dados, method=metodo, headers=cabecalhos)
    try:
        with urllib.request.urlopen(pedido, timeout=30) as r:
            resposta = r.read()
    except urllib.error.HTTPError as e:
        raise ErroImmich(f"O Immich respondeu {e.code}." + (" Confirma a chave da API." if e.code in (401, 403) else "")) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErroImmich(f"Sem ligação ao Immich ({e}).") from e
    if binario:
        return resposta
    return json.loads(resposta.decode()) if resposta else {}


def testar(chave: str) -> str:
    eu = _pedido("/api/users/me", chave=chave)
    return f"Ligação ao Immich confirmada: {eu.get('name') or eu.get('email') or 'utilizador'}."


# ------------------------------------------------------------------ fotos de um período
@dataclass
class Foto:
    id: str
    nome: str
    quando: datetime | None          # hora local da foto
    cidade: str | None
    pais: str | None
    video: bool = False

    @property
    def link(self) -> str:
        return f"{url_browser()}/photos/{self.id}"


@dataclass
class Fotos:
    fotos: list = field(default_factory=list)
    total: int = 0

    @property
    def lugares(self) -> list[tuple[str, int]]:
        """[(«Albufeira, Portugal», n)] dos mais fotografados para os menos."""
        c = Counter(", ".join(x for x in (f.cidade, f.pais) if x) for f in self.fotos if f.cidade or f.pais)
        return c.most_common()

    @property
    def paises(self) -> list[str]:
        return [p for p, _ in Counter(f.pais for f in self.fotos if f.pais).most_common()]


def _datahora(txt) -> datetime | None:
    try:
        return datetime.fromisoformat(str(txt).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def fotos_entre(ini: date, fim: date, *, chave: str | None, maximo: int = 1000) -> Fotos:
    """Fotos e vídeos tirados entre duas datas (pela hora local da foto; a pesquisa alarga 12 h para os fusos)."""
    depois = datetime.combine(ini, datetime.min.time()) - timedelta(hours=12)
    antes = datetime.combine(fim, datetime.max.time()) + timedelta(hours=12)
    out, pagina = Fotos(), 1
    while pagina and len(out.fotos) < maximo:
        r = _pedido("/api/search/metadata", "POST", {
            "takenAfter": depois.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "takenBefore": antes.strftime("%Y-%m-%dT%H:%M:%S.999Z"),
            "page": pagina, "size": 250, "withExif": True, "order": "asc"}, chave=chave)
        bloco = r.get("assets") or {}
        for a in bloco.get("items", []):
            quando = _datahora(a.get("localDateTime") or a.get("fileCreatedAt"))
            if quando and not (ini <= quando.date() <= fim):
                continue
            exif = a.get("exifInfo") or {}
            out.fotos.append(Foto(a["id"], a.get("originalFileName") or a["id"], quando, exif.get("city"), exif.get("country"),
                                  a.get("type") == "VIDEO"))
        proxima = bloco.get("nextPage")
        pagina = int(proxima) if proxima and str(proxima).isdigit() else None
    out.fotos.sort(key=lambda f: f.quando or datetime.min)
    out.total = len(out.fotos)
    return out


def miniatura(aid: str, *, chave: str | None) -> bytes:
    return _pedido(f"/api/assets/{aid}/thumbnail?size=thumbnail", binario=True, chave=chave)


# ------------------------------------------------------------------ álbum da viagem
def album_existe(album_id: str, *, chave: str | None) -> dict | None:
    try:
        return _pedido(f"/api/albums/{album_id}?withoutAssets=true", chave=chave)
    except ErroImmich as e:
        if "404" in str(e) or "400" in str(e):
            return None
        raise


def criar_album(nome: str, descricao: str, ids: list[str], *, chave: str | None) -> str:
    r = _pedido("/api/albums", "POST", {"albumName": nome[:120], "description": descricao[:500], "assetIds": ids}, chave=chave)
    if not r.get("id"):
        raise ErroImmich("O Immich não devolveu o álbum criado.")
    return r["id"]


def juntar_ao_album(album_id: str, ids: list[str], *, chave: str | None) -> int:
    """Junta as fotos que faltam no álbum. Devolve quantas entraram."""
    if not ids:
        return 0
    r = _pedido(f"/api/albums/{album_id}/assets", "PUT", {"ids": ids}, chave=chave)
    return sum(1 for x in r if isinstance(x, dict) and x.get("success")) if isinstance(r, list) else 0


def link_album(album_id: str) -> str:
    return f"{url_browser()}/albums/{album_id}"
