#!/bin/sh
# Instala (ou atualiza) o Paperless-ngx na NAS, ao lado do Gestor de Contas. Correr NA NAS, por SSH:
#   sh /share/Container/paperless/instalar_paperless.sh
#
# - Paperless-ngx (porta 8300) + Redis, com OCR em português, base de dados SQLite (sem Postgres).
# - Rede Docker partilhada «nas-apps»: o Gestor fala com o Paperless por http://paperless:8000 (nesta NAS os
#   containers não se conseguem alcançar pelo IP da própria NAS — ver QNAP-Container-Apps/SETUP.md).
# - Pasta de entrada: /share/Container/paperless/consume. ATENÇÃO: o Paperless APAGA os ficheiros desta pasta
#   depois de os importar — nunca a apontes para a tua área de documentos; usa copiar_documentos.sh, que copia.
# Correr outra vez atualiza para a imagem mais recente (os dados ficam).
set -e

BASE="/share/Container/paperless"
REDE="nas-apps"
IMAGEM="ghcr.io/paperless-ngx/paperless-ngx:latest"
IP_NAS="${IP_NAS:-192.168.1.232}"

mkdir -p "$BASE/data" "$BASE/media" "$BASE/export" "$BASE/consume" "$BASE/redis"
# o Paperless corre dentro do container com o utilizador 1000: tem de poder escrever nas suas pastas
chown -R 1000:1000 "$BASE/data" "$BASE/media" "$BASE/export" "$BASE/consume"
chmod 775 "$BASE/consume"

# Configuração (criada na 1.ª vez; depois edita-se à mão). A chave secreta é gerada aqui e nunca sai da NAS.
CONF="$BASE/paperless.conf"
if [ ! -f "$CONF" ]; then
  SEGREDO=$(head -c 48 /dev/urandom | base64 | tr -d '\n/+=' | head -c 50)
  cat > "$CONF" <<EOF
PAPERLESS_REDIS=redis://paperless-redis:6379
PAPERLESS_SECRET_KEY=$SEGREDO
PAPERLESS_URL=http://$IP_NAS:8300
PAPERLESS_TIME_ZONE=Europe/Lisbon
PAPERLESS_OCR_LANGUAGES=por
PAPERLESS_OCR_LANGUAGE=por+eng
PAPERLESS_DATE_ORDER=DMY
PAPERLESS_CONSUMER_RECURSIVE=true
PAPERLESS_CONSUMER_SUBDIRS_AS_TAGS=true
PAPERLESS_FILENAME_FORMAT={{ created_year }}/{{ correspondent }}/{{ title }}
EOF
  chmod 600 "$CONF"
  echo "Configuração criada em $CONF"
fi

docker network inspect "$REDE" >/dev/null 2>&1 || docker network create "$REDE"

echo "A obter as imagens..."
docker pull redis:7
docker pull "$IMAGEM"

echo "A (re)criar os containers..."
docker rm -f paperless-redis paperless 2>/dev/null || true
docker run -d --name paperless-redis --restart unless-stopped --network "$REDE" \
  -v "$BASE/redis:/data" redis:7
docker run -d --name paperless --restart unless-stopped --network "$REDE" \
  -p 8300:8000 \
  -v "$BASE/data:/usr/src/paperless/data" \
  -v "$BASE/media:/usr/src/paperless/media" \
  -v "$BASE/export:/usr/src/paperless/export" \
  -v "$BASE/consume:/usr/src/paperless/consume" \
  --env-file "$CONF" \
  "$IMAGEM"

echo
echo "Feito. O 1.º arranque demora alguns minutos (instala o OCR em português)."
echo "Abre http://$IP_NAS:8300 quando responder."
echo
echo "Se for a primeira instalação, cria o teu utilizador (a password é pedida no terminal):"
echo "  docker exec -it paperless python3 manage.py createsuperuser"
