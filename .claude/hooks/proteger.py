"""PreToolUse (Edit|Write): impede o Claude de mexer em dados reais, segredos e migrações já aplicadas.

Bloqueia (exit 2, com a razão para o Claude):
- tudo dentro de data/ (dados reais da app)
- *.db, *.db-wal, *.db-shm (bases de dados SQLite)
- secret.key e *.env / .env* (chaves e segredos, ex.: ia.env com ANTHROPIC_API_KEY)
- migrations/versions/*.py que já estejam no git: uma migração aplicada nunca se altera, cria-se outra
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stderr.reconfigure(encoding="utf-8")  # no Windows a consola não é UTF-8 e os acentos estragavam-se
try:
    dados = json.load(sys.stdin)
except Exception:
    sys.exit(0)

caminho = (dados.get("tool_input") or {}).get("file_path") or ""
if not caminho:
    sys.exit(0)

raiz = Path(os.environ.get("CLAUDE_PROJECT_DIR") or dados.get("cwd") or ".").resolve()
alvo = Path(caminho)
if not alvo.is_absolute():
    alvo = raiz / alvo
alvo = alvo.resolve()
try:
    rel = alvo.relative_to(raiz)
except ValueError:
    sys.exit(0)  # fora do projecto: não é connosco

partes = [p.lower() for p in rel.parts]
nome = alvo.name.lower()


def bloquear(razao: str) -> None:
    print(f"Bloqueado pelo hook do projecto ({rel.as_posix()}): {razao}", file=sys.stderr)
    sys.exit(2)


if partes and partes[0] == "data":
    bloquear("data/ tem os dados reais da app; não se editam à mão.")
if nome.endswith((".db", ".db-wal", ".db-shm")):
    bloquear("é uma base de dados SQLite; altera-a através da app ou de uma migração.")
if nome == "secret.key" or nome.endswith(".env") or nome.startswith(".env"):
    bloquear("é um ficheiro de chaves/segredos; o utilizador é que o edita.")
if len(partes) == 3 and partes[:2] == ["migrations", "versions"] and nome.endswith(".py"):
    seguido = subprocess.run(["git", "ls-files", "--error-unmatch", rel.as_posix()],
                             cwd=raiz, capture_output=True).returncode == 0
    if seguido:
        bloquear("esta migração já está no git (e provavelmente aplicada na NAS). "
                 "Não a alteres: cria uma migração nova em migrations/versions/.")

sys.exit(0)
