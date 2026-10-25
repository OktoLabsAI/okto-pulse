# Plano de ativação do Readiness persistido

Estado: revisão antes da janela de migração. Este documento descreve uma operação futura; não executa a migração da instância ativa.

## Escopo e pré-condições

- Binários Core e Community precisam estar no mesmo par de commits validado para o release. A versão local exibida é 0.4.2; o formato SQLite alvo da branch é `user_version=403`.
- O upgrade admite somente `user_version=400`, `401` ou `402` com `application_id` e fingerprint exatos. Qualquer formato desconhecido ou alterado é recusado, sem DDL parcial.
- A instância Pulse e todos os demais escritores do mesmo `DATA_DIR` devem estar parados. Confirmar caminho absoluto do banco, identidade do runtime, ausência de novos writes e `quick_check`/`foreign_key_check` antes da janela.
- O ensaio isolado mais recente do banco ativo v401 preservou 153 tabelas de fonte e 56.040 linhas, verificou o backup e resultou em v403. Esses números são evidência histórica do ensaio, não pré-condição para a janela; repetir a checagem na cópia mais recente.

## Ensaio isolado antes da janela

1. Registrar `DATA_DIR`, caminho do `pulse.db`, versão do pacote, hash dos dois commits e horário. Não usar o board `okto-grafx` como fixture de carga.
2. Executar `python scripts/validate_readiness_upgrade.py <caminho-absoluto-do-pulse.db>` a partir do checkout Community que será instalado. O script usa `sqlite3.Connection.backup()` para copiar a fonte em modo leitura, migra apenas a cópia temporária e compara hash e contagem de cada tabela de fonte antes/depois e no backup retido.
3. Exigir `source_unchanged=true`, `backup_verified=true`, `upgraded_schema_version=403`, `quick_check=ok` e `foreign_key_check` vazio. Se falhar, não iniciar a janela.
4. Em banco descartável v403, iniciar somente o worker de readiness e observar reconciliação paginada, criação de `readiness_scope`/`readiness_job`, publicação de pelo menos uma geração e GET com `freshness=current`. Testar restart durante backfill e avanço da página seguinte; GET não deve criar job.

## Janela de instalação

1. Parar Pulse e confirmar encerramento dos processos de API/MCP e ausência de escritor no banco. Conservar o pacote anterior e registrar o último checkpoint de fonte.
2. Criar backup operacional consistente separado do arquivo produzido pelo upgrader; guardar também os arquivos WAL/SHM apenas conforme o procedimento SQLite de backup, sem cópia a quente por `Copy-Item`. Verificar integridade do backup antes de seguir.
3. Executar `python scripts/upgrade_readiness_schema.py <caminho-absoluto-do-pulse.db>` com o novo pacote. O upgrader toma `BEGIN EXCLUSIVE` com timeout zero, refaz fingerprint e integridade, cria backup próprio, aplica apenas as tabelas/triggers de readiness e só então confirma `user_version=403`.
4. Conferir `application_id`, `user_version=403`, `quick_check=ok`, `foreign_key_check` vazio, presença das tabelas e triggers de readiness e mesmas contagens/hashes das tabelas de fonte. Guardar caminho e hash do backup pré-upgrade.
5. Iniciar o novo Pulse. A reconciliação cria jobs de backfill em páginas de até 100 Specs por tick; o worker calcula fora de transação de escrita e publica uma geração completa. Durante a primeira passagem, a UI pode mostrar `pending_initial`; não converter ausência em 0%.
6. Verificar API/MCP e UI em board de teste autorizado: `generation_id`, `source_revision=target_revision`, `freshness=current`, I/V, linhas de Coverage e tempo de resposta. Observar idade da dívida, erros terminais, pool/WAL e progresso entre Specs. Fazer somente leituras no `okto-grafx` durante a conferência.

## Falha e retorno

- Antes do primeiro write no schema novo, parar o serviço, preservar o banco falho para diagnóstico e restaurar o backup pré-upgrade verificado. Reinstalar o par de binários anterior; nunca iniciar binário antigo sobre schema v403 incompatível.
- Depois de qualquer write novo, não restaurar o backup antigo sobre o banco atual. Isso perderia reports, provas e alterações de Card/Spec. Preservar ambos, reconciliar o delta de fonte de forma auditável e só então decidir recuperação ou manter o novo binário em modo de leitura compatível. Não existe rollback automático seguro após writes novos.
- Falha de um job não autoriza reabrir Done nem alterar provas. Corrigir a causa e usar o requeue autenticado, com `edition`, revisão alvo e auditoria; consultar o runbook `READINESS_PERSISTED_RECOVERY.md`.

## Gatilho de execução

A migração e o reinício da instância ativa ocorrem somente após revisão deste plano, conclusão das TASK06–TASK09, validação dos wheels e autorização já condicionada pelo usuário à revisão. Nenhum passo de instalação deve antecipar esse gatilho.
