#!/bin/sh
# Corre este script NA NAS (por SSH) para atualizar o container para a imagem
# mais recente publicada no GHCR.
#
# Usa "docker" puro (não "docker compose"): nesta NAS, "docker compose" falha
# com um erro de permissão sob contas de admin secundárias (ex: snunes_adm) —
# ver C:\Projetos\QNAP-Container-Apps\SETUP.md secção 6/7 para detalhes.
# As migrações da base de dados correm sozinhas no arranque do container.
set -e

IMAGE="ghcr.io/seriocha/gestor-contas:latest"
NAME="gestor-contas"
BASE="/share/Container/gestor-contas"

mkdir -p "$BASE/data" "$BASE/backup"

# Arquivo de extratos numa pasta partilhada (opcional): o caminho vem de
# $BASE/arquivo.conf, ex.:  echo /share/Documentos/Gestor-Contas > $BASE/arquivo.conf
ARQUIVO_OPTS=""
if [ -f "$BASE/arquivo.conf" ]; then
  ARQUIVO="$(head -n 1 "$BASE/arquivo.conf" | tr -d '\r')"
  case "$ARQUIVO" in
    *" "*) echo "ERRO: o caminho em arquivo.conf não pode ter espaços: $ARQUIVO"; exit 1 ;;
  esac
  if [ -n "$ARQUIVO" ]; then
    mkdir -p "$ARQUIVO"
    ARQUIVO_OPTS="-v $ARQUIVO:/arquivo:rw -e GESTOR_ARQUIVO_DIR=/arquivo"
    echo "Arquivo de extratos: $ARQUIVO -> /arquivo"
  fi
fi

echo "A obter a imagem mais recente ($IMAGE)..."
docker pull "$IMAGE"

echo "A recriar o container..."
docker rm -f "$NAME" 2>/dev/null || true
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  -p 8200:8000 \
  -e TZ=Europe/Lisbon \
  -e GESTOR_COOKIE_SECURE=0 \
  -e GESTOR_BACKUP_INTERVALO_HORAS=24 \
  -e GESTOR_BACKUP_RETENCAO_DIAS=30 \
  -v "$BASE/data:/data:rw" \
  -v "$BASE/backup:/backup:rw" \
  $ARQUIVO_OPTS \
  "$IMAGE"

echo "A limpar imagens antigas..."
docker image prune -f

echo "Feito. Container a correr:"
docker ps | grep "$NAME"
