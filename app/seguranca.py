"""Passwords (argon2), bloqueio após tentativas falhadas e token CSRF."""
import secrets
import time
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request

_ph = PasswordHasher()

PASSWORD_MIN = 10
MAX_FALHAS = 5
BLOQUEIO_SEGUNDOS = 15 * 60


def hash_password(password: str) -> str:
    return _ph.hash(password)


@lru_cache(maxsize=1)
def _hash_ficticio() -> str:
    return _ph.hash(secrets.token_hex(16))


def verificar_password(password_hash: str | None, password: str) -> bool:
    """Para logins inexistentes verifica contra um hash fictício, para o tempo
    de resposta não revelar se o utilizador existe."""
    try:
        return _ph.verify(password_hash or _hash_ficticio(), password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validar_password_nova(password: str, confirmacao: str) -> str | None:
    if password != confirmacao:
        return "As passwords não coincidem."
    if len(password) < PASSWORD_MIN:
        return f"A password tem de ter pelo menos {PASSWORD_MIN} caracteres."
    return None


class Bloqueios:
    """Contador em memória de logins falhados por nome de utilizador."""

    def __init__(self):
        self._falhas: dict[str, tuple[int, float]] = {}

    def segundos_restantes(self, login: str) -> int:
        n, ate = self._falhas.get(login.lower(), (0, 0.0))
        return max(0, int(ate - time.time())) if n >= MAX_FALHAS else 0

    def falha(self, login: str) -> None:
        chave = login.lower()
        n, ate = self._falhas.get(chave, (0, 0.0))
        if n >= MAX_FALHAS and ate <= time.time():
            n = 0  # bloqueio anterior expirou
        n += 1
        self._falhas[chave] = (n, time.time() + BLOQUEIO_SEGUNDOS if n >= MAX_FALHAS else 0.0)

    def sucesso(self, login: str) -> None:
        self._falhas.pop(login.lower(), None)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def ler_form(request: Request):
    """Lê o formulário de um POST e valida o token CSRF."""
    form = await request.form()
    esperado = request.session.get("csrf")
    if not esperado or not secrets.compare_digest(str(form.get("csrf", "")), esperado):
        raise HTTPException(status_code=400, detail="Pedido inválido (CSRF). Recarrega a página.")
    return form
