# Readiness persistido: diagnóstico e retomada

Este runbook vale para o worker de percentuais e cobertura da Spec. Ele não
altera as provas de entrega, os gates de Done nem o KG.

## Identificar a situação

1. Consulte a projeção autenticada da Spec. Registre `edition`,
   `source_revision`/revisão alvo, `generation_id`, `freshness` e `error_code`.
2. Consulte os logs `readiness.queue`, `readiness.queue_lag`,
   `readiness.projection_failed` e `readiness.requeued`. Os logs de requeue
   incluem scope, revisão e ID de auditoria, sem o motivo ou conteúdo de prova.
3. `updating` conserva a última geração coerente enquanto há dívida.
   `pending_initial` significa que ainda não há uma geração válida.
   `unavailable` com erro terminal requer corrigir a causa antes de requeue.
4. Um processo interrompido deixa o lease expirar em até 30 segundos. O
   próximo claim usa um epoch maior. Staging abandonado não é visível e a
   limpeza o remove em lotes. A reconciliação percorre até 100 Specs por tick
   com cursor persistido e roda a cada 60 segundos.

## Reprocessar um job terminal

Após corrigir a causa, um administrador do board pode executar:

```http
POST /api/v1/boards/{board_id}/specs/{spec_id}/readiness/requeue
Content-Type: application/json

{
  "edition": 1,
  "expected_target_revision": 42,
  "idempotency_key": "repair-incident-123",
  "reason": "Storage available after incident 123"
}
```

Use a edição e a revisão alvo observadas imediatamente antes do comando.
`202` devolve `scope`, `requested_revision`, `queued`, `audit_id` e `replayed`.
Repetir a mesma chave e o mesmo conteúdo devolve o mesmo `audit_id`; uma
edição ou revisão alterada exige nova leitura e retorna `409`. Sem a permissão
`readiness.requeue`, o comando retorna `403`. Scope inexistente retorna `404`.
Uma falha de persistência retorna erro e não confirma enfileiramento.

O comando existe também como
`okto_pulse_requeue_persisted_delivery_readiness` no MCP, com os mesmos
campos. Não edite manualmente `readiness_job`, não apague a fonte para limpar
a fila e não use GET como reparo. Se o erro persistir após oito tentativas,
o job volta a terminal preservando revisão, erro e última geração válida.

## Conferir recuperação

Consulte novamente a projeção até `freshness=current`, com
`captured_revision=source_revision`. Confira que a nova geração possui
summary e linhas da mesma geração. Se a fonte mudar durante o cálculo,
`updating` é esperado até a revisão mais recente ser publicada. O ID da
auditoria permite correlacionar o requeue sem expor conteúdo de prova.
