# Deploy na QNAP

Passos genéricos da NAS (SSH, GHCR público, porque é que `docker compose`
falha por SSH): `C:\Projetos\QNAP-Container-Apps\SETUP.md`. Aqui só o que é
desta app.

| | |
|---|---|
| Imagem | `ghcr.io/seriocha/gestor-contas` |
| Porta | **8200** (interna 8000) |
| Dados | `/share/Container/gestor-contas/data` → `/data` (`gestor.db`, `secret.key`) |
| Backups | `/share/Container/gestor-contas/backup` → `/backup` |

## Primeira instalação

1. No PC: `./release.sh 0.1.0` e esperar pelo build no GitHub Actions.
2. Tornar o pacote GHCR público (SETUP.md §5) — só expõe o código, nunca dados.
3. Na NAS (File Station ou SSH): criar `/share/Container/gestor-contas/data` e `.../backup`.
4. Container Station → **Create Application** → colar `qnap/docker-compose.yml`.
5. Abrir `http://<IP_NAS>:8200` → página de configuração inicial → criar o administrador.
6. Administração → Utilizadores → criar a conta da tua mulher (por omissão: *Editar* na Família, sem acesso à Mãe).

## Atualizar

```bash
ssh snunes_adm@192.168.1.232
sh /share/Container/gestor-contas/update.sh
```
(copiar `qnap/update.sh` para essa pasta na 1.ª vez.)

## Segurança

- **Não abrir a porta 8200 no router.** Acesso de fora só por VPN (QVPN/Tailscale).
- Com HTTPS (reverse proxy do QTS ou Tailscale), pôr `GESTOR_COOKIE_SECURE=1`.
- `secret.key` assina as sessões: apagá-lo termina todas as sessões (não perde dados).
- Incluir `/share/Container/gestor-contas/backup` num job HBS3 para fora da NAS.

## Restaurar um backup

```bash
docker stop gestor-contas
cp /share/Container/gestor-contas/backup/gestor-AAAAMMDD-HHMMSS.db /share/Container/gestor-contas/data/gestor.db
rm -f /share/Container/gestor-contas/data/gestor.db-wal /share/Container/gestor-contas/data/gestor.db-shm
docker start gestor-contas
```
