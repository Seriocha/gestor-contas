#!/bin/sh
# Cria as subpastas dos extratos bancários (uma por banco) para a leitura automática do Gestor de Contas.
# Correr NA NAS, por SSH, com um utilizador administrador:
#   sh criar_pastas_extratos.sh                    -> usa /share/Extratos
#   EXTRATOS_DIR=/share/Outra sh criar_pastas_extratos.sh
#
# A pasta partilhada em si (para aparecer na rede como \\NAS\Extratos) cria-se no QTS:
#   Painel de Controlo › Privilégios › Pastas partilhadas › Criar › Pasta partilhada, nome «Extratos».
# Se ainda não existir, este script não a inventa: avisa e, se quiseres, usa uma alternativa dentro da
# partilha «Container» que já existe (\\NAS\Container\extratos).
set -e

EXTRATOS_DIR="${EXTRATOS_DIR:-/share/Extratos}"
BANCOS="Santander ActivoBank Revolut BiG CreditoAgricola"

if [ ! -d "$EXTRATOS_DIR" ]; then
  echo "A pasta $EXTRATOS_DIR não existe."
  echo "  1) Cria a pasta partilhada «Extratos» no QTS (ver o início deste script) e corre-o outra vez; ou"
  echo "  2) usa a alternativa dentro da partilha Container:"
  echo "       EXTRATOS_DIR=/share/Container/extratos sh $0 --criar"
  echo "     e depois: EXTRATOS_DIR=/share/Container/extratos sh /share/Container/gestor-contas/update.sh"
  if [ "$1" != "--criar" ]; then
    exit 1
  fi
  mkdir -p "$EXTRATOS_DIR"
  echo "Criada $EXTRATOS_DIR"
fi

for banco in $BANCOS; do
  if [ -d "$EXTRATOS_DIR/$banco" ]; then
    echo "  já existe  $EXTRATOS_DIR/$banco"
  else
    mkdir -p "$EXTRATOS_DIR/$banco"
    echo "  criada     $EXTRATOS_DIR/$banco"
  fi
done

# Leitura e escrita para os utilizadores (pôr ficheiros pela rede) e para o container (mover para importados/).
chmod -R ug+rwX "$EXTRATOS_DIR"

echo
echo "Feito. Põe cada extrato na pasta do seu banco, por exemplo:"
echo "  $EXTRATOS_DIR/Santander/Movimentos outubro.xls"
echo "Depois corre (uma vez) o update.sh para o container passar a ver a pasta:"
if [ "$EXTRATOS_DIR" = "/share/Extratos" ]; then
  echo "  sh /share/Container/gestor-contas/update.sh"
else
  echo "  EXTRATOS_DIR=$EXTRATOS_DIR sh /share/Container/gestor-contas/update.sh"
fi
