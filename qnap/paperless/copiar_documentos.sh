#!/bin/sh
# Copia os documentos que já tens numa pasta partilhada da NAS para a pasta de entrada do Paperless.
# Os ORIGINAIS NÃO SÃO MEXIDOS: o Paperless importa as cópias (e apaga-as da pasta de entrada).
# As subpastas passam a etiquetas no Paperless (ex.: Seguros/, Casa/Garantias/ → etiquetas «Seguros», «Casa», «Garantias»).
#
# Uso, NA NAS, por SSH (caminho sem espaços):
#   DOCS_DIR=/share/Documentos sh /share/Container/paperless/copiar_documentos.sh
# Pode correr-se outra vez: os documentos já importados são reconhecidos pelo conteúdo e ignorados pelo Paperless.
# Formatos lidos pelo Paperless (sem extras): PDF, imagens (JPG, PNG, TIFF…), texto. Word/Excel precisam do Tika.
set -e

DOCS_DIR="${DOCS_DIR:?Indica a pasta dos documentos: DOCS_DIR=/share/Documentos sh $0}"
ENTRADA="/share/Container/paperless/consume"

[ -d "$DOCS_DIR" ] || { echo "A pasta $DOCS_DIR não existe."; exit 1; }
[ -d "$ENTRADA" ] || { echo "O Paperless ainda não está instalado (falta $ENTRADA). Corre primeiro instalar_paperless.sh."; exit 1; }

N=$(find "$DOCS_DIR" -type f \( -iname '*.pdf' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.tif' -o -iname '*.tiff' -o -iname '*.webp' -o -iname '*.txt' \) \
      -not -path '*/@*' -not -path '*/#recycle/*' -not -path '*/.*' | wc -l)
echo "A copiar $N documentos de $DOCS_DIR para a entrada do Paperless..."

# rsync copia preservando as subpastas (que viram etiquetas) e salta as pastas de sistema do QTS
rsync -a --prune-empty-dirs \
  --exclude='@*' --exclude='#recycle' --exclude='.*' \
  --include='*/' \
  --include='*.pdf' --include='*.PDF' --include='*.jpg' --include='*.JPG' --include='*.jpeg' --include='*.JPEG' \
  --include='*.png' --include='*.PNG' --include='*.tif' --include='*.tiff' --include='*.TIF' --include='*.webp' --include='*.txt' \
  --exclude='*' \
  "$DOCS_DIR"/ "$ENTRADA"/
chown -R 1000:1000 "$ENTRADA" && chmod -R ug+rwX "$ENTRADA"   # o Paperless (utilizador 1000) lê e apaga as cópias

echo "Feito. O Paperless vai importando (OCR) aos poucos; acompanha em http://192.168.1.232:8300 › Tarefas."
