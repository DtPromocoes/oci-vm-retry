# oci-vm-retry

Fica tentando criar automaticamente uma VM Always Free na Oracle Cloud (OCI)
a cada 5 minutos, via GitHub Actions, ate conseguir. Quando a VM e criada
(ou quando ela ja existe), o proprio workflow se desativa.

## Secrets necessarios

Configure estes 6 secrets no repositorio (Settings > Secrets and variables > Actions):

- `OCI_USER` — OCID do usuario
- `OCI_TENANCY` — OCID da tenancy
- `OCI_FINGERPRINT` — fingerprint da chave de API
- `OCI_REGION` — regiao OCI
- `OCI_KEY` — conteudo da chave privada PEM
- `SSH_PUBKEY` — chave publica SSH que sera autorizada na VM

## Funcionamento

- O workflow roda a cada 5 minutos (`*/5 * * * *`) e tambem pode ser
  disparado manualmente.
- Se a VM configurada ja existir, ou se a criacao tiver sucesso, o job
  grava uma saida indicando que o proprio workflow deve ser desativado
  (`gh workflow disable`).
- Erros de falta de capacidade ("Out of host capacity") sao esperados e
  tratados como normais: o script tenta novamente no proximo ciclo.

## Seguranca dos logs

Como este repositorio e publico, o script nunca imprime OCIDs, IP
publico, regiao, usuario, tenancy, fingerprint ou qualquer outro dado
sensivel. Antes de qualquer saida, esses valores sao mascarados com
`::add-mask::`, e as mensagens de log sao sempre genericas.
