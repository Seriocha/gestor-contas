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

# Sugestões por IA (opcional): chave da Claude API num ficheiro só legível pelo admin, ex.:
#   printf 'ANTHROPIC_API_KEY=sk-ant-...\n' > $BASE/ia.env && chmod 600 $BASE/ia.env
IA_OPTS=""
if [ -f "$BASE/ia.env" ]; then
  IA_OPTS="--env-file $BASE/ia.env"
  echo "Sugestões por IA: chave lida de $BASE/ia.env"
fi

# Extratos bancários (opcional): pasta partilhada da NAS com uma subpasta por banco (Santander/, ActivoBank/…).
# A app lê-a periodicamente e os ficheiros validados passam para <banco>/importados/. Outra pasta:
#   EXTRATOS_DIR=/share/Outra/Pasta sh update.sh       (caminho sem espaços)
EXTRATOS_DIR="${EXTRATOS_DIR:-/share/Extratos}"
EXTRATOS_OPTS=""
if [ -d "$EXTRATOS_DIR" ]; then
  EXTRATOS_OPTS="-v $EXTRATOS_DIR:/importar:rw -e GESTOR_IMPORT_DIR=/importar"
  echo "Extratos: pasta $EXTRATOS_DIR montada em /importar"
else
  echo "Extratos: $EXTRATOS_DIR não existe — leitura automática desligada"
fi

# Paperless-ngx (opcional): ligação ao Paperless da NAS (qnap/paperless/). O token da API fica num ficheiro só
# legível pelo admin (ver qnap/README.md › Paperless-ngx). Os dois containers ficam na rede «nas-apps»
# (nesta NAS os containers não se alcançam pelo IP da própria NAS).
PAPERLESS_OPTS=""
if [ -f "$BASE/paperless.env" ]; then
  PAPERLESS_OPTS="--env-file $BASE/paperless.env"
  echo "Paperless: ligação lida de $BASE/paperless.env"
fi
# Immich (opcional): fotografias das viagens. Ligação em $BASE/immich.env (ver qnap/README.md › Immich). O container
# do Immich tem de estar também na rede «nas-apps» (docker network connect nas-apps <container do Immich>).
IMMICH_OPTS=""
if [ -f "$BASE/immich.env" ]; then
  IMMICH_OPTS="--env-file $BASE/immich.env"
  echo "Immich: ligação lida de $BASE/immich.env"
fi
docker network inspect nas-apps >/dev/null 2>&1 || docker network create nas-apps

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
  $IA_OPTS \
  $EXTRATOS_OPTS \
  $PAPERLESS_OPTS \
  $IMMICH_OPTS \
  --network nas-apps \
  "$IMAGE"

echo "A limpar imagens antigas..."
docker image prune -f

echo "Feito. Container a correr:"
docker ps | grep "$NAME"
