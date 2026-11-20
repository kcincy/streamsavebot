# Limpeza e continuidade operacional

## Dados protegidos

- database/bot.sqlite3: banco ativo, integralmente migrado.
- database/backups/before-layout.sqlite3: backup consistente antes da reorganizacao.
- database/backups/.env.before-layout: copia privada das configuracoes.
- database/telegram-bot-api/: sessao atual do servidor, movida com verificacao SHA-256.
- database/backups/legacy-api-state/: copia verificada da sessao antiga externa.
- cache/spool/: somente captura atual, envios ou recuperacoes pendentes; nao remover manualmente.
- logs/: atividade atual e historico de servicos preservado em logs/legacy.

As tabelas de usuarios, streamers, inscricoes, entregas e stop_requests foram comparadas antes e depois:
os dados e estados permaneceram identicos. Referencias do spool foram atualizadas na mesma transacao.
O banco passou em PRAGMA integrity_check. Nenhum usuario precisa cadastrar streamers novamente.

## Removido ou consolidado

Os modulos Python foram movidos para src/. O iniciador main.py ficou pequeno e permanece na raiz.
Foram removidos o .gitignore e o utilitario manual obsoleto tests/validate_follow_live.py.
A pasta recordings estava vazia. Caches pip, bytecodes e temporarios antigos foram isolados em
cache/obsolete-runtime, cache/obsolete-bytecode e cache/obsolete-recordings.

## Restricao da limpeza fisica

A revisao automatica bloqueou comandos de exclusao recursiva, inclusive para o cache local.
A resposta foi apenas "blocked by policy", sem justificativa mais detalhada. As exclusoes nao foram
contornadas nem contabilizadas como realizadas. O registro exato esta em cache/cleanup-report.json.

Permanecem aproximadamente 5,24 GB de residuos verificados: cerca de 5,20 GB nas pastas antigas
build, build-vs, source, vcpkg, runtime e logs de:

C:\Users\ycnick\AppData\Local\SuperLiveTelegramBotAPI

O restante esta isolado nos cache/obsolete-* locais. O servidor atual usa o executavel e DLLs de
tools/telegram-bot-api/bin; nenhum modulo do processo depende dessas compilacoes externas.
Os binarios reaproveitados foram comparados por hash com a instalacao antiga.

Foi preparado scripts/cleanup.ps1 para execucao manual pelo usuario. Ele valida os alvos exatos,
recusa uma instalacao antiga em uso e preserva banco, configuracao, fila e sessao. Nao e iniciado
pelo bot e nao foi executado pelo agente. A estimativa de residuos nao equivale a espaco liberado.

Ferramentas compartilhadas de sistema, Python base, Visual Studio e caches globais de outros
projetos foram preservados. O projeto nao depende de pacotes Python globais nem de FFmpeg global.

## Validacao operacional

Consulte logs/bot.log para monitoramentos restaurados, estados reais de perfil/captura e confirmacoes
de envio. A fila pendente foi preservada e retomada automaticamente apos a mudanca de pastas.
Uma mensagem de envio confirmado e a gravacao do message_id no banco precedem a limpeza do arquivo.
