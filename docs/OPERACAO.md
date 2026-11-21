# Operação — versão 2026.11.19.1

A versão 2026.11.19.2 adiciona marca d'água opcional, atualmente ativada com o texto
`teste`. Ela exige reencodificação e aumenta o custo de processamento, mantendo as
dimensões e os frames. Configuração, medições e limites: [WATERMARK.md](WATERMARK.md).

Atualização 2026.11.19.3: não há cota semanal nem `/limits`. O cadastro comum continua
limitado a três perfis; Enviar fecha a parte e mantém a captura, Remover exclui a inscrição.
Os nomes SuperLive no monitor incluem o ID. A marca é controlada por `WATERMARK_ENABLED`
(`true` / `false`) no `.env`, com o texto mantido no `settings.env`.

## O que entra nesta versão

Partes de **até 300 MB decimais**, com qualidade original. O buffer continua limitado a
16 GiB, com **52 reservas** compartilhadas entre capturas e partes na fila. As três
margens globais de processamento, corte e cache continuam separadas. `MAX_ACTIVE_CAPTURES=100`
é um teto de segurança, não uma promessa de capacidade: os recursos e o buffer podem
permitir bem menos. A proteção também se aplica ao superusuário.

### Admissão e recuperação

- Medição a cada 5 segundos. CPU relativa à cota real do contêiner; com 6 CPUs, 80% equivale
  a 4,8 núcleos. Memória usa o consumo do cgroup descontando cache de arquivo inativo
  recuperável; pouca memória disponível na VM também bloqueia admissões.
- Uma inicialização por vez, com pelo menos 15 segundos entre entradas. A próxima aguarda
  os primeiros dados da anterior, evitando acumular vários bootstraps caros.
- CPU ou memória a 80% já impedem uma nova entrada. Após 15 segundos nessa faixa, a
  proteção só libera novas entradas depois de 30 segundos abaixo de 65% CPU e 70% memória.
- Memória a 90% por 5 segundos, ou CPU a 95% por 60 segundos: finalizar a captura admitida
  mais recentemente. No máximo uma suspensão a cada 30 segundos; o perfil espera pelo menos
  60 segundos antes de tentar novamente. A parte capturada é entregue normalmente.
- Sem ao menos 1 GiB ou quatro partes de espaço livre (o maior dos dois), nenhuma nova
  captura começa; as existentes são finalizadas para reservar espaço ao processamento e aos
  envios. O espaço livre considera o volume Linux e as pastas compartilhadas do Windows,
  usando o menor valor. Métricas indisponíveis também bloqueiam novas entradas.
- Prioridade para contas com menos capturas ativas; depois, ordem de chegada. Uma captura
  pode atender vários usuários. A posição pode mudar quando entram usuários menos atendidos.
  A fila é persistida no SQLite, e entradas offline, canceladas ou sem interessados saem dela.
- Verificações de perfis têm concorrência 2. Cinco falhas consecutivas por plataforma
  aumentam o intervalo de reconexão; dados gravados com sucesso zeram o contador.
- O watchdog finaliza e reinicia a sessão de um perfil que fique 90 segundos sem crescimento
  do arquivo, ou 120 segundos sem concluir uma inicialização admitida. O FFmpeg de captura
  também tem proteção própria contra fonte sem progresso e pode ser encerrado à força
  depois de dois segundos sem responder ao encerramento normal, preservando tags completas.
  Não reinicia todo o navegador para recuperar um único perfil.

`/monitor` informa posição e espera por recursos em inglês ou português. Espera não
consome tempo de gravação no histórico. Trechos transmitidos durante a espera não são recuperáveis.

### Retenção e envios

O antigo prazo de uma hora passa a disparar um alerta de atraso: **não apaga gravações
pendentes, falhas de processamento ou uploads sem confirmação**. Arquivos entregues são
limpos normalmente. Se os pendentes encherem o buffer, novas partes aguardam espaço;
não há descarte automático para abrir vagas. Pendências sem solução precisam de revisão
operacional e podem manter o buffer cheio.

Envios alternam usuários, preservando a ordem das partes de cada perfil para cada destinatário.
Retentativas respeitam o estado e os limites do Telegram. Envios ambíguos continuam sem
repetição automática, para evitar duplicações. Comandos têm limite por usuário de 20
solicitações em rajada, com reposição de 2 por segundo. Avisos de excesso têm intervalo
mínimo de 10 segundos.

Alertas operacionais são enviados ao superusuário cadastrado, no idioma salvo, com limite
de um aviso por motivo a cada 15 minutos. Abrangem pressão de recursos, captura sem progresso,
processamento, atraso de entrega e falha de backup. **Nenhum painel ou comando administrativo
foi adicionado**; a etapa 3 permanece adiada.

### TikTok — mudança de configuração de vídeo

Fragmentos com menos de 10 segundos são ignorados antes da criação do contact sheet,
sem aparecer como falha de processamento. Um único frame sem duração e sem pacotes de
áudio também é tratado como fragmento curto; durações desconhecidas de outros vídeos
continuam preservadas para revisão, sem envio automático.

O caminho anterior mantinha a configuração inicial de vídeo ao dividir o MP4 fragmentado.
Agora o TikTok é remuxado por cópia em FLV, preservando os cabeçalhos AVC/HEVC e AAC.
Ao mudar um cabeçalho, a parte anterior fecha e a próxima começa em um quadro-chave com
a configuração atual. As partes são preparadas para MP4 no envio, preservando a resolução
de cada trecho, sem forçar uma qualidade inferior nem reencodar o vídeo compatível.

Foi confirmado erro de decodificação H.264 no arquivo entregue do perfil
6804923004176565253, iniciado em 19/11/2026 às 19:05:07. A mídia das 18:53:44 foi localizada
no catálogo, mas o Telegram não disponibilizou seu arquivo na tentativa de recuperação.
A causa exata dessas duas gravações antigas não pôde ser comprovada sem a fonte bruta.
A mudança de configuração é uma falha reproduzida e corrigida no novo fluxo. Cabeçalhos
perdidos não podem ser reconstruídos com segurança a partir do vídeo corrompido sozinho.
O arquivo recuperado está preservado em `cache/incidents/tiktok-20261119/`, no Windows e
no volume Docker. As nove extrações de frames do sheet agora abortam se houver erro de
decodificação; uma falha preserva o bruto, impede o envio automático e alerta o administrador.
Essa verificação é amostral: não decodifica todo o vídeo nem garante detectar defeitos entre amostras.

### Backups locais

Primeira tentativa 30 segundos após a inicialização, em baixa carga; depois, a cada 24 horas.
Uma falha preserva os backups anteriores e tenta novamente em uma hora. Durante a cópia,
novas capturas, processamento e envios aguardam; gravações já em andamento continuam.
O backup aguarda processamento ou envio em curso terminar antes de começar.

Os arquivos `production-*.zip` ficam diretamente em **database/backups**, no Windows,
por bind mount. São mantidos os três últimos arquivos verificados. Outros backups manuais
da pasta não são removidos. Cada arquivo contém:

- snapshots consistentes de ambos os SQLite, pela API de backup;
- imagens dos sheets;
- arquivos já finalizados ainda necessários à fila;
- código Python e configurações disponíveis no contêiner;
- manifesto SHA-256, CRC do ZIP e verificação `integrity_check` dos bancos.

Credenciais, cookies, sessões de autenticação e capturas ainda em andamento são excluídos.
Não é um arquivo permanente de todos os vídeos. Na perda completa do ambiente pode ser
necessário autenticar novamente. A cópia fora do computador foi adiada por decisão do usuário.

A criação exige espaço para arquivo, verificação e todo o buffer de captura, com mais
1 GiB de margem. Se não houver espaço, o backup falha com alerta sem apagar o anterior.

Verificar um backup, sem tocar no banco ativo:

```powershell
docker compose exec -T bot python -B /app/src/backups.py /app/database/backups/NOME.zip
```

Testar extração para uma pasta **nova ou vazia**, sem substituir a produção:

```powershell
docker compose exec -T bot python -B /app/src/backups.py /app/database/backups/NOME.zip --restore-to /app/cache/restore-check
```

Para restauração efetiva, parar o bot, preservar os volumes atuais e restaurar bancos,
imagens e spool nos mesmos caminhos do contêiner. Nunca sobrescrever SQLite em uso.
Como os arquivos usam caminhos `/app/...`, restaurá-los em uma pasta de teste não altera
as referências do banco para essa pasta.

### Saúde, atualização e validação

O heartbeat é salvo em `cache/health.json` no volume Docker, com contadores de captura,
fila e recursos. O healthcheck do Compose verifica heartbeat recente e Bot API local.
O estado `unhealthy` sinaliza falha; Docker Compose não reinicia automaticamente apenas
por esse estado. Serviços internos encerrados inesperadamente provocam encerramento
coordenado e a política `restart: unless-stopped` continua aplicável.

Antes de atualizar: backup verificado, testes em contêiner separado sem credenciais nem
volumes de produção e versão identificada no log. Alterações só em Python ainda permitem
reinício; alterações no Compose exigem recriação, e dependências exigem build.

```powershell
docker compose config --quiet
docker compose build bot
docker compose up -d --no-build
docker compose ps
docker compose exec -T bot tail -n 60 /app/logs/bot.log
```

Não usar `down -v`, não executar outra instância com o mesmo token e não forçar encerramento
durante upload. Os backups de código anteriores permitem revisão e retorno controlado;
a migração desta versão só adiciona dados de fila e justiça de envio, sem apagar inscrições.

O teste contínuo de 48–72 horas e a comprovação de 100 lives reais simultâneas ainda são
critérios de liberação para uma abertura ampla. A fila protege a máquina, mas não aumenta
sua capacidade de CPU, RAM, rede ou disco. Linux dedicado pode ser avaliado após as medições.
