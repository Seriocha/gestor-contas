"""PostToolUse (Edit|Write): corre só os testes do módulo editado.

app/eredes.py            -> tests/test_eredes.py
app/parsers/revolut_pdf.py -> tests/test_revolut_pdf.py
tests/test_x.py          -> o próprio ficheiro

Se não houver ficheiro de testes correspondente, não faz nada. Se os testes falharem,
devolve o fim do output ao Claude (exit 2) para ele corrigir logo.
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
if not caminho.endswith(".py"):
    sys.exit(0)

raiz = Path(os.environ.get("CLAUDE_PROJECT_DIR") or dados.get("cwd") or ".").resolve()
alvo = Path(caminho)
if not alvo.is_absolute():
    alvo = raiz / alvo
try:
    rel = alvo.resolve().relative_to(raiz)
except ValueError:
    sys.exit(0)

topo = rel.parts[0] if rel.parts else ""
if topo == "tests" and rel.name.startswith("test_"):
    teste = rel
elif topo == "app" and rel.name != "__init__.py":
    teste = Path("tests") / f"test_{rel.stem}.py"
else:
    sys.exit(0)

if not (raiz / teste).is_file():
    sys.exit(0)

try:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                        "-W", "ignore::DeprecationWarning", teste.as_posix()],
                       cwd=raiz, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=170)
except subprocess.TimeoutExpired:
    print(f"{teste.as_posix()}: os testes passaram o limite de tempo do hook.", file=sys.stderr)
    sys.exit(0)

if r.returncode in (0, 5):  # 5 = nenhum teste recolhido
    sys.exit(0)

saida = (r.stdout + r.stderr).strip().splitlines()
print(f"Os testes de {teste.as_posix()} falharam depois desta edição:\n" + "\n".join(saida[-40:]),
      file=sys.stderr)
sys.exit(2)
