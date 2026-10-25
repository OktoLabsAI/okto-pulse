# Readiness: matriz de fontes e invalidação

O worker lê um snapshot de `Spec`, `Board`, `Card`, evidência de entrega, histórico
da Spec, Target, execução, receipt e revogação. Cada mutação relacional dessas
fontes passa por triggers SQLite instalados por
`readiness_invalidation_schema.py`. A trigger grava `readiness_scope.source_revision`
e faz upsert de `readiness_job.requested_revision` **na transação do comando**.
Assim, um erro de gravação do intent aborta a própria mutação. O worker e o KG
não participam desse commit.

| Fonte lida pelo avaliador | Escritores suportados | Escopo invalidado | Prova |
| --- | --- | --- | --- |
| `specs` (semântica, edição, cenários, execução, archive) | comandos de Spec/resultado/rebase | Spec atual; edição antiga termina ao mudar edition; archive termina o job | `test_failed_scenario_result_invalidates_before_evidence_binding`, `test_new_edition_tombstones_old_generation_scope`, `test_archive_terminates_debt_and_restore_requeues_current_scope` |
| `spec_history` | histórico dos comandos de Spec | Spec do histórico | `test_board_policy_fans_out_and_spec_history_invalidates_only_its_spec` |
| `boards` (settings/realm relevantes à política) | configuração do Board | Specs do Board | `test_board_policy_fans_out_and_spec_history_invalidates_only_its_spec` |
| `cards` (população, status, links de cenário, report) | criação/edição/transição/archive/reparent | Spec do Card; reparent invalida origem e destino | `test_reparenting_invalidates_both_scopes`, `test_intent_write_failure_rolls_back_source_update` |
| `delivery_evidence_records` e `card_delivery_evidence_records` | waiver, implementação, teste, progress, inspeção, revogação, batch/report | Spec e edição do registro | `test_authenticated_report_and_exact_replay_share_source_transaction`, `test_delivery_batch.py`, `test_delivery_report.py` |
| `implementation_targets`, `implementation_target_resolutions`, `implementation_target_execution_records` | Target, resolução e resultado de execução | Spec do Card correspondente | `test_target_execution_receipt_and_revocation_invalidate_consumer` |
| `code_investigation_receipts` e `code_investigation_receipt_revocations` | receipt e revogação | Specs com execução ligada ao receipt | `test_target_execution_receipt_and_revocation_invalidate_consumer` cobre revogação; receipt novo só passa a ser consumido ao ligar execução |

Replays exatos de evidência não inserem novo registro e não avançam a revisão.
Um resultado de cenário `failed` altera `specs.test_scenarios`, portanto já
invalida antes de qualquer binding. A trigger de Card invalida o escopo antigo
e o novo quando `spec_id` ou `board_id` muda. Archive marca o scope como
`tombstone` e termina seu job; a restauração reabre a dívida. A migração v403
também repara jobs pendentes de scopes já arquivados. Troca de edição termina
o job antigo. A exclusão física da Spec
remove o scope por FK cascade, logo uma publicação tardia falha no fence em vez
de recriá-lo. Dados da edição anterior preservados no histórico continuam sem
crédito na edição atual.

As tabelas de identidade e ACL são lidas por requisição, antes de entregar a
projeção, e não entram no snapshot do worker. Alterar permissões revoga acesso
imediatamente sem recalcular porcentagens. `WAL`, `synchronous=FULL` e
`foreign_keys=ON` são configurados em `sqlalchemy_database.py`.

## Limites ainda abertos

- A cobertura acima demonstra os caminhos principais e os triggers instalados.
  Os testes de Target/execução/receipt exercitam a admissão SQL e o fence de
  revisão numa fixture; a prova de cada transporte público continua nas suítes
  de integração dos respectivos comandos.
- Os registros de evidência, Targets e receipts são imutáveis ou versionados nos
  comandos públicos. Uma atualização SQL direta que troque a relação de um
  desses registros entre Specs não invalida o escopo antigo; isso deve ser
  protegido por testes e triggers antes de declarar suporte a tal operação.
- A exclusão física não retém uma linha tombstone histórica. A semântica de
  publicação está protegida pelo desaparecimento do scope e pelo FK; se a
  retenção da lápide for requisito literal, exige evolução adicional do schema.
- O tempo de commit com falha de intent e sem KG foi observado nos testes; as
  metas de latência sob a fixture normativa ainda não foram atingidas.
