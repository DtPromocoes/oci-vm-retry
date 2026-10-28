# oci-vm-retry

Fica tentando criar automaticamente uma VM Always Free na Oracle Cloud (OCI)
via GitHub Actions, praticamente sem parar, ate conseguir. Quando a VM e
criada (ou quando ela ja existe), o proprio workflow se desativa.

## Como funciona

- Cada execucao do workflow roda um job longo (ate ~5h40m de orcamento,
  `timeout-minutes: 355`) que tenta criar a VM em ciclos de aproximadamente
  60 segundos, em vez de depender só do agendamento do GitHub Actions.
- Antes de **cada** tentativa de `launch_instance` (em cada combinacao de
  shape e dominio de disponibilidade), o script verifica de novo se a VM ja
  existe. Isso garante que nunca sejam criadas duas VMs, mesmo que uma
  chamada anterior tenha dado timeout ou erro de rede sem o script saber se
  a instancia foi criada do lado da OCI.
- Falta de capacidade ("Out of host capacity") e tratada como normal: conta
  no resumo do ciclo e o script tenta de novo no proximo ciclo.
- Erros 429 (limite de taxa) interrompem o ciclo atual e acionam um backoff
  exponencial (120s, 240s, 480s, limitado por `BACKOFF_MAX`), que volta ao
  normal depois de um ciclo sem 429.
- Erros de autenticacao/configuracao (401/403/404/400, exceto falta de
  capacidade) encerram o job com falha, pois exigem correcao manual dos
  secrets ou de alguma politica da OCI.
- Outros erros (rede, timeout, 5xx diversos) sao logados de forma resumida
  e o script segue para o proximo ciclo.
- Quando o orcamento de tempo do job termina sem sucesso, o workflow dispara
  automaticamente a proxima execucao (`gh workflow run`), que fica na fila
  do mesmo grupo de concorrencia e so comeca quando a anterior terminar —
  nunca ha duas execucoes rodando ao mesmo tempo.
- Existe tambem um agendamento de reserva a cada 30 minutos
  (`*/30 * * * *`), para o caso da corrente de auto-encadeamento se romper
  por algum motivo (falha de infraestrutura do GitHub, etc.).
- Quando a VM e criada com sucesso, ou quando ela ja existe, o workflow se
  desativa (`gh workflow disable`) e nao se auto-encadeia mais.

## Secrets necessarios

Configure estes 6 secrets no repositorio (Settings > Secrets and variables > Actions):

- `OCI_USER` — OCID do usuario
- `OCI_TENANCY` — OCID da tenancy
- `OCI_FINGERPRINT` — fingerprint da chave de API
- `OCI_REGION` — regiao OCI
- `OCI_KEY` — conteudo da chave privada PEM
- `SSH_PUBKEY` — chave publica SSH que sera autorizada na VM

## Variaveis configuraveis (opcionais)

- `VM_NAME` (padrao `dtpromo`) — nome da instancia a ser criada/verificada.
- `BUDGET_SECONDS` (padrao `20400`, ~5h40m) — orcamento de tempo de cada job.
- `CYCLE_SLEEP` (padrao `60`) — pausa entre ciclos sem erro de taxa.
- `SUMMARY_EVERY` (padrao `600`) — intervalo para imprimir o resumo de progresso.
- `BACKOFF_MAX` (padrao `600`) — teto do backoff exponencial apos 429.

## Seguranca dos logs

Como este repositorio e publico, o script nunca imprime OCIDs, IP
publico, regiao, usuario, tenancy, fingerprint ou qualquer outro dado
sensivel. Antes de qualquer saida, esses valores sao mascarados com
`::add-mask::`, e as mensagens de log sao sempre genericas (tipo de erro,
status HTTP e codigo, nunca a mensagem completa).
