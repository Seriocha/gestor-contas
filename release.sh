#!/usr/bin/env bash
# Faz uma nova release: cria uma tag git e envia-a para o GitHub.
# O GitHub Actions apanha a tag e publica automaticamente a imagem no GHCR.
#
# Uso:
#   ./release.sh 0.2.0
set -euo pipefail

if [ -z "${1:-}" ]; then
  echo "Uso: ./release.sh <versao>   ex: ./release.sh 0.2.0"
  exit 1
fi

VERSION="$1"
TAG="v${VERSION}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [ -n "$(git status --porcelain)" ]; then
  echo "Há alterações por commitar. Faz commit primeiro:"
  git status --short
  exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "A tag $TAG já existe."
  exit 1
fi

git tag -a "$TAG" -m "Release $TAG"
git push origin "$BRANCH"
git push origin "$TAG"

echo ""
echo "Tag $TAG enviada. Acompanha o build em:"
echo "  https://github.com/Seriocha/gestor-contas/actions"
echo ""
echo "Quando terminar, a imagem fica disponível em:"
echo "  ghcr.io/seriocha/gestor-contas:$VERSION"
echo "  ghcr.io/seriocha/gestor-contas:latest"
echo ""
echo "Depois, na NAS: ./update.sh (dentro da pasta qnap/) para atualizar o container."
