# KG: execução Community no pacote integrado v1.3

Este documento substitui as instruções antigas de v0.3.4 e acompanha
`feature/v0.4.0`. A autoridade é o pacote consolidado
`Pulse_Plano_Codex_v1_3_Arquitetura_Verificabilidade` e seus quatro documentos,
começando por `INICIAR_NO_CODEX_ENTREGA.md`. O mapa integrado D/G/Q vive no
Core em `docs/KG_TRACEABILITY_GAPS_PLAN.md`; estado/provas no
`docs/pulse-simplification/IMPLEMENTATION_LEDGER.md`. Não consultar a ideação
original nem tratar esta edição como projeto separado.

Este é um mapa de obrigações, não declaração de conclusão. O histórico do plano
anterior permanece no Git; contagens e observações antigas não provam runtime
atual. O runtime de grafo é Okto Grafx.

## Fronteira executável

SQLAlchemy, Grafx, filesystem, scheduler, HTTP e telemetria concreta vivem nos
adapters Community. O Core expõe Protocols e semântica. Um adapter usa a porta
pública; se falta contrato, criar a porta no Core. Não importar módulos privados
do Core, duplicar policy ou abrir exceção transitória. Todos os oito budgets de
`okto-pulse-saas-closure` permanecem ZERO.

## Schema, recuperação e retirada de Sprint

O contrato 0.7.0 foi implementado nos dois repos: 82 layouts, sem novo nome de
relação, acrescentando `derives_from` Requirement→Constraint e
Constraint→Constraint para referências admitidas pelo domínio. Mantém 49 colunas
por nó de domínio, embedding na posição exigida, BoardMeta e vetores. Os
fingerprints e o delta exato estão em `GRAPH_SCHEMA_070.md` no Core.

Preservar contratos históricos 0.5.0/0.6.0, backup autenticado e recibos v2.
Evolução para 0.7.0 usa candidato separado e recibo v3 revalidado integralmente.
Não aplicar ALTER/upgrade silencioso num banco versionado, reinterpretar recibo
antigo, falsificar datas/UUID/cursor nativo ou liberar runtime com candidato
incompleto. O bootstrap corrente deve recusar o predecessor sem mutá-lo.

A cadeia interna combina migração relacional de Sprint, permissões/arquivo,
snapshot final, projeção determinística, fontes cognitivas, reconciliação,
checkpoint, publicação, admissão e rollback. Não há tool MCP, rota REST, botão,
CLI ou override público de manutenção. Dados reais não são fixtures; migrar ou
parar runtime real exige autorização específica.

## Mecânica de projeção

- Carregar inputs completos e atuais pela porta; transportar identidade,
  versões, status e datas da fonte. Ausência/ambiguidade não vira conjunto vazio
  autorizado a remover relações.
- Reconciliar intents fechados por owner/namespace e famílias autorizadas pelo
  Core. Preservar regras, seções, writers e owners alheios. Capturar before-images
  antes de mutar; confirmar remoção e compensar falhas após escrita.
- Manter Card como dono de card→filhos, resolvendo Entity/Bug corretamente.
  Não acrescentar dialeto Grafx ao Core nem fabricar raízes parciais para ligar
  referências externas.
- Eventos alcançam ambos os consumidores pertinentes. Preparação relê a fonte;
  evento atrasado não restaura vínculo removido. Qualificar concorrência,
  commit/ACK, replay e paridade incremental/rebuild por conjuntos.
- Pendências distinguem lacuna semântica, atraso, permissão, ambiguidade e erro
  técnico. Correção passa pelo domínio; não criar fila obrigatória de reparo ou
  edição livre de arestas como atalho de autoridade.

## Learnings e policy

Persistir captura semântica admitida no store durável/revisionado existente antes
da materialização quando necessário. Conclusão/outbox e revalidação de versão
devem respeitar a unidade de trabalho real; não prometer atomicidade entre
stores que a stack não oferece. Preserve conteúdo histórico ao reabrir ou
superseder. Similaridade oferece candidatos; não decide supersedência sozinha.

Não registrar worker de LLM interna nem exigir configuração de LLM para o
backend inventar Learning. Advisory/blocking é policy humana de Board;
executor não altera seu próprio gate. Blocking avalia captura/lacuna semântica
atual, não falta de materialização, fila, DLQ ou dívida antiga. Migração não
transforma advisory em blocking.

## REST, consultas e frontend

REST e MCP compartilham contratos, permissões, completude e frescor. Leituras
não reparam, reconciliam, fazem backfill ou criam conhecimento. Não retornar
vazio como prova de ausência quando a fonte está incompleta/inacessível.
Timeout e limites precisam chegar à execução Grafx, inclusive agregações;
limites do pacote são 15/30 s e 200/1000 linhas. Não contar perguntas por agente;
preservar isolamento e controle de concorrência.

Implementar Impacto na Decision, Cobertura na Spec e Clusters em Analytics,
reutilizando tabs/painéis existentes. Health mostra estado técnico e limitações;
não vira console de reparo ou tela de julgamento semântico. Mostrar direção,
regra, confiança, camada, fonte e limites pertinentes. Settings de consulta são
configuração de Board, não tuning público do armazenamento.

Cobertura deve refletir obrigações diretas/herdadas e crédito específico por
Card, com o read model relacional autoritativo. Não usar um `supports` ou teste
funcional passing para substituir prova técnica/operacional exigida. Nenhuma
nova leitura, checklist ou aprovação obrigatória para abrir um painel.

Atualizar separadamente versão do schema de armazenamento e contrato de health.
Testar cada feature que impacta frontend, incluindo texto de versão, filtros,
estados incompletos e permissões. Rebuild da SPA e `verify:frontend-dist` fazem
parte da distribuição; um teste JSDOM não comprova pixels do canvas.

## Qualificação e entrega pareada

Usar Grafx e SQL reais descartáveis, falhas injetadas, before-images, replays,
backups e processos frescos. Cobrir KG-01–KG-66 em conjunto com plano-base,
entrega incremental e arquitetura/verificabilidade. Preservar os resultados
falhos e separar reexecuções focadas de campanhas completas.

Construir wheels dos dois checkouts locais, instalar o par e comparar todos os
`.py` byte a byte antes dos testes comportamentais. Reiniciar somente processos
próprios de teste quando a prova exigir; reinstalar não atualiza processo vivo.
Congelar pares instalados para campanhas longas e não atribuir seu resultado
a commits posteriores.

Gerar catálogo MCP no Core e manifests pelos geradores oficiais. Mudanças de
imports exigem matriz README produzida pela auditoria, nunca editada à mão.
Atualizar changelogs, contratos de UI e documentação conforme o comportamento
verificado. Benchmark mede o fluxo completo sem cortar gates. Publicação,
merge e migração real não são consequência automática de testes verdes.
