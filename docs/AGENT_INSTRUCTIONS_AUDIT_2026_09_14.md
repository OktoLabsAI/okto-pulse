# Auditoria das instruções para agentes — 2026-09-14

## Resultado e escopo

O Pulse já tem uma boa base de navegação por domínio, contratos tipados e leitura
progressiva. O principal problema não é ausência de instruções: é a divergência
entre camadas que descrevem a mesma regra, combinada com roteiros extensos.
Recomenda-se corrigir inconsistências antes de ampliar o protocolo.

Os achados abaixo preservam a avaliação inicial. A rodada AI-01..AI-10 foi
implementada posteriormente, conforme o registro de entrega ao final. Nenhum
board, política, permissão ou processo do Pulse foi alterado. A melhoria
anterior sobre cópia/referência já foi publicada no Core
em `feature/v0.3.3`, commit `d6e5c8d`.

Baseline examinado: Core checkout `okto-pulse-core-kg5-codex` em `ec654c3`
(equivalente à publicação documental `d6e5c8d`) e Community `6cbfd2d`.

Cobertura:

- Inventário e varredura estrutural de todos os 57 Markdown dos resources Core
  (695.572 bytes), prompt inicial e quatro substituições operacionais Community
  (106.470 bytes). Esses números incluem índices e duplicações; não são o custo
  de contexto de uma sessão nem uma contagem de tokens.
- Conferência de nomes documentados com funções do servidor por AST: todas as
  272 funções `okto_pulse_*` definidas em `server.py` têm seção em tool-docs.
  A comparação identificou 340 títulos de ferramentas; famílias registradas em
  módulos separados impedem tratar diferenças como ferramentas inexistentes.
- Revisão semântica dirigida dos fluxos SDLC, preflight, cobertura, evidência,
  qualidade, permissões, segurança, respostas, erros e composição Community.
- Testes de instruções, preflight e catálogo executados sem usar boards reais.

Limites: não é uma certificação linha a linha de cada argumento/exemplo contra
todos os handlers, nem um E2E de todas as ferramentas. Guidelines particulares
dos boards e arquivos de prompt customizados por instalação não foram auditados.
A superfície pode ser substituída por `OKTO_PULSE_AGENT_INSTRUCTIONS_PATH`;
uma futura validação de instalação deve registrar o catálogo/prompt efetivo.

Nos links relativos abaixo, `../src` é Community; Core é o repositório irmão
`../../okto-pulse-core-kg5-codex`. Os caminhos Core referem-se ao checkout auditado.

## Achados confirmados e oportunidades

### AI-01 — Classificação de evidências legadas contraditória [alta; baixo esforço]

O bootstrap diz que classificação é humana via UI/REST, sem mutação MCP.
O preflight repete a proibição. A referência detalhada permite agentes
autorizados e existe uma ferramenta implementada para isso.

Evidências: [bootstrap](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/agent_instructions.md),
[preflight](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/workflows/preflight.md),
[classificação](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/reference/code_traceability.md)
seção “Legacy classification is explicit actor governance”, e
[handler](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/code_traceability_tools.py)
`okto_pulse_classify_legacy_code_evidence`.

Impacto: agente pode parar e pedir intervenção humana desnecessária ou receber
orientações incompatíveis. Alinhar a instrução à autoridade implementada, sem
conceder permissões novas e sem permitir inferência de proveniência.

### AI-02 — Sobreposição Community perde orientação comum [alta; médio esforço]

Community substitui integralmente quatro resources de mesma URI. A referência
operacional de erros não inclui 21 códigos `code_investigation_*` presentes no
documento Core, incluindo conflito de versão, expiração, idempotência, origem e
digest. Isso não remove os validadores, mas remove orientação da URI que o
bootstrap aponta como canônica.

Evidências: [composição](../src/okto_pulse/community/adapters/resources.py)
`_COMMUNITY_REPLACEMENT_TABLE`, [erros Community](../src/okto_pulse/community/resources/operational/reference/errors.md)
e [erros Core](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/reference/errors.md).

Recomendação: compor seções comuns e específicas ou sincronizar explicitamente
as regras comuns enquanto se mantém o mecanismo atual. Testar a documentação
efetivamente servida em Community, não apenas arquivos Core. Não eliminar
diferenças legítimas entre adaptadores nem introduzir Grafx no Core.

### AI-03 — `outcome` externo e interno confundidos [alta; baixo esforço]

A referência de projeções apresenta `ok|error` como chave canônica. Mais abaixo,
menciona o envelope MCP V2. O envelope externo implementado usa
`success|action_required|error`; o significado de sucesso de transporte não
equivale à conclusão de uma operação assíncrona ou aprovação de um gate.

Evidências: [projeções](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/reference/projection_profiles.md)
e [McpToolOutcome](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/outcome.py).

Recomendação: mostrar um exemplo real de envelope externo + `data` + projeção
interna, com instrução para `action_required`, bloqueio e resultado aceito ainda
não terminal. Não mudar a API; corrigir sua explicação.

### AI-04 — Hierarquia de confiança incompleta [alta; baixo esforço]

O bootstrap exige leitura dos resources, mas sua seção de segurança diz que
“Only this file + board guidelines count as trusted instructions”. Isso exclui
literalmente os próprios resources prescritos. A referência de KB corretamente
distingue material não confiável de artefatos com autoridade de produto.

Evidências: [bootstrap, Security](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/agent_instructions.md)
e [Knowledge Governance](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/reference/knowledge-governance.md).

Recomendação: separar protocolo oficial do catálogo, política autorizada do
board, requisitos de produto e conteúdo externo. Requisitos orientam a entrega,
mas não concedem autoridade para executar comandos ou ignorar segurança.
Instruções Pulse não devem se apresentar como superiores às regras do ambiente
do agente ou à autorização do usuário.

### AI-05 — Estados resumidos não descrevem todas as alternativas [média; baixo esforço]

O preflight manda `move_card(in_progress)` sem qualificar o estado inicial.
A tabela de cards normais descreve apenas `not_started → started → in_progress`.
Há inclusive permissão `card.move.not_started_to_in_progress`; portanto não se
deve concluir, apenas pela tabela, que o início direto é proibido.

Evidências: [preflight](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/workflows/preflight.md),
[transições](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/reference/transitions.md)
e [permissões](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/domain/permissions.py).

Recomendação: roteiro dependente do estado/tipo/permissões, consultando
`get_allowed_transitions`, com distinção entre caminho usual e conjunto de
arestas permitido. Remover “It is always correct”: uma consulta é um snapshot,
não uma autorização que permanece válida após mudanças concorrentes.

### AI-06 — Concluir teste confundido com teste aprovado [alta; baixo esforço documental]

`card_types` e `transitions` dizem que todos os cenários devem estar `passed`
ou `automated`. O domínio descreve `passed, failed, or automated`; o preflight
de transições trata `draft|ready` ou evidência insuficiente como pendência.
Um teste executado com falha precisa continuar sendo representado honestamente.

Evidências: [card_types](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/reference/card_types.md),
[domínio](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/domain/card_transition.py)
`test_completion_block`, [readiness](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/application/use_cases/allowed_transitions.py)
montagem de `pending_scenarios`.

Recomendação: documentar separadamente “execução do Test Card concluída”,
“resultado do cenário”, “evidência autenticada” e “aprovação da entrega”.
Confirmar o caso com teste do comando de conclusão antes de fixar o texto;
não converter `failed` em `passed` para satisfazer uma mensagem genérica.

### AI-07 — Protocolos obrigatórios versus gates [média; baixo esforço]

Project Structure é corretamente identificado como obrigação do agente, não
gate do servidor. Essa distinção não é uniforme nas demais regras MUST: leituras
de contexto, consultas KG, comentários e anexação podem parecer o mesmo tipo de
bloqueio. O sumário Resource Gate também não certifica todas as obrigações.

Evidências: [Project Structure](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/reference/project_structure.md),
[preflight](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/workflows/preflight.md),
[ideations](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/workflows/ideations.md).

Recomendação: rotular cada regra como gate, protocolo, advisory ou ação humana;
indicar onde ler a configuração efetiva e qual evidência comprova cumprimento.
Preservar as regras existentes, sem criar novos gates.

### AI-08 — Custo de leitura e sequência de trabalho [média; médio esforço]

Há leitura progressiva já implementada; preservá-la. Entretanto, o workflow de
cards tem 33.276 bytes, Spec Gates 32.621, Code Traceability 46.073 e tool-docs KG
63.928. O bootstrap simultaneamente manda cachear e reler ao mudar de domínio.
Regras e exceções de versão, edição, receipt e idempotência ficam distribuídas.

Recomendação: folha operacional curta por ação (entrada, leituras, mutação,
verificação e parada), com referências detalhadas sob demanda; cache por
identidade/hash do catálogo e nova leitura quando mudar a versão efetiva.
Contexto mutável de entidades continua sujeito à atualização antes do gate.
Separar claramente versão do produto, versão do resource, edição da entidade,
versão do conteúdo e revisão do head. Não confundir otimização de leitura com
dispensa de contexto necessário ou execução cega de mutações em lote.

### AI-09 — Reparos e operações assíncronas precisam de um protocolo comum [média; médio esforço]

As regras de CAS e idempotência são boas em Quality/RDL/evidências, mas estão
espalhadas. Recuperação do grafo e Discovery têm escopos e meios distintos.
Não basta uma instrução genérica de retry.

Evidências: [Quality](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/reference/quality-assessments.md),
[RDL](../../okto-pulse-core-kg5-codex/src/okto_pulse/core/mcp/resources/workflows/refinements.md),
[KG Community](../src/okto_pulse/community/resources/operational/workflows/kg.md).

Recomendação: matriz curta: entrada inválida → corrigir; CAS → reler e nova
operação; replay exato → mesma chave; permissão → solicitar ator autorizado;
resultado incerto → consultar estado antes de repetir; accepted → acompanhar o
handle até terminal; recovery → usar somente o fluxo do componente diagnosticado.
Explicitar limites de polling e os dados mínimos para escalar ao usuário.

### AI-10 — Testes atuais garantem estrutura, não ausência de contradições [alta; médio esforço]

Os testes de instrução concatenam diversos arquivos e procuram frases.
Encontrar uma regra correta em algum arquivo não prova que o roteiro consultado
pelo agente está correto, nem detecta sua negação em outra camada.

Evidência: [test_agent_instructions_auto_copy.py](../../okto-pulse-core-kg5-codex/tests/test_agent_instructions_auto_copy.py).

Recomendação: testes de contratos documentais por URI efetiva e casos curtos:
iniciar Task; terminar teste falho com evidência; classificar legado autorizado;
interpretar `action_required`; copiar mockup preservando origem; reagir a CAS;
tratar KB advisory; distinguir aceite de job de sua conclusão. Validar exemplos
contra schemas/serviços em fixtures isoladas. Manter os guards estruturais.

## Sequência recomendada e critério de conclusão

1. Corrigir AI-01/03/04 e confirmar AI-06 com teste pontual.
2. Alinhar AI-05/07 sem alterar políticas nem conceder permissões.
3. Resolver a perda de instruções comuns AI-02 e validar Core + Community.
4. Consolidar AI-08/09 e fechar com testes AI-10.

Escopo fechado: dez itens acima. Novas descobertas ficam registradas à parte;
não expandir silenciosamente esta rodada. DoD: documentação consistente com
contratos vigentes, exemplos executáveis em fixtures, catálogo efetivo testado,
sem regressão das regras de segurança, proveniência e independência de revisão.

## Validação realizada

- `test_agent_instructions_auto_copy.py` + `test_preflight_resource_drift_guard.py`:
  **27 passaram** (2,13 s reportados pelo pytest).
- `test_r11a_resource_catalog.py` + `test_mcp_resources.py`:
  **160 passaram** (4,86 s reportados pelo pytest).
- Total **187 testes pontuais**, sem regressão completa do produto e sem E2E
  mutante nos boards. A venv Community não tinha pytest; a execução bem-sucedida
  usou Python 3.13 global com `PYTHONPATH` apontando para o Core auditado.
- Passar esses testes não invalida os achados semânticos acima.

## Entrega AI-01..AI-10 — implementada e validada

| Item | Entrega |
|---|---|
| AI-01 | Removida a proibição desatualizada em sete rotas de instrução. Classificação MCP exige a permissão existente, proveniência e CAS; não transforma V1 em V2. |
| AI-02 | Erros Community sincronizados integralmente com Core, seguidos de notas específicas preservadas. Restauradas duas seções comuns do workflow KG e seis de tool-docs KG. Teste via cliente MCP verifica os textos efetivamente servidos e recusa perda/contradição do prefixo comum de erros. |
| AI-03 | Envelope externo `success/action_required/error` separado da projeção interna `ok/error`; exemplo JSON comparado com `McpToolOutcome` real. |
| AI-04 | Hierarquia distingue protocolo oficial, política autorizada, intenção do produto e dados não confiáveis, sem sobrepor regras do ambiente/autorização do usuário. |
| AI-05 | Início/retrabalho condicionado à aresta permitida, tipo, estado e gates; readiness tratado como snapshot, não autorização durável. |
| AI-06 | Test Card concluído não significa teste aprovado. Cenário falho preservado em teste de conclusão real do serviço; mensagens/readiness/templates gerados não preselecionam aprovação. |
| AI-07 | Quadro de categorias distingue gate, protocolo, advisory e autoridade humana/independente, com origem de configuração e prova. |
| AI-08 | Mapa curto por ação e glossário de identidades no preflight; cache de instruções por catálogo/conteúdo sem cache indevido de contexto mutável. Bootstrap condensado para 10.539 caracteres (LF), mantendo referências de detalhe. |
| AI-09 | Protocolo comum de CAS, replay, timeout incerto, jobs, cancelamento, backoff/polling e escalonamento, sem ampliar autoridade. |
| AI-10 | Guards por URI efetiva, exemplo vinculado ao contrato executável, cenários positivos/negativos de identidade, permissões e evidência, além de cliente MCP isolado Community. |

### Contratos e manutenção

- Não foram alterados enums de status, permissões, configurações de gates ou
  critérios de aprovação. O Core permanece agnóstico ao backend de grafo.
- A alteração executável é instrucional em `services/gate_contracts.py`:
  `next_action.params_template.status` agora contém
  `<observed_terminal_status>`, em vez de sugerir `passed` incondicionalmente;
  `status_choices` é metadado de orientação separado dos argumentos da tool.
  Campos-placeholder devem ser substituídos antes da chamada. A assinatura da
  ferramenta de cenário não mudou. Mensagens de readiness descrevem o resultado
  terminal sem convertê-lo em aprovação.
- Escolhida **sincronização explícita**, não mudança do mecanismo de providers.
  Ao editar erros comuns do Core, sincronizar o prefixo Community; o teste de
  overlay falha se qualquer regra comum for perdida ou alterada. Se uma seção
  comum nova aparecer nas quatro URIs substituídas, atualizar o documento
  operacional. Diferenças legítimas de backend permanecem em Community.
- Instruções são servidas da instalação e do catálogo congelado. As mudanças
  foram instaladas nas duas instalações locais e o Pulse reiniciado em
  2026-09-14 junto com a correção de disponibilidade do Board KG Analytics.
  Os 42 testes de inicialização/recursos Community passaram novamente antes
  da instalação, e os arquivos instalados foram comparados com os wheels.
  Ainda não houve commit/push destas mudanças; `d6e5c8d` é a entrega anterior.

### Evidência de validação da entrega

- **322 testes Core passaram**: instruções/preflight, contratos de resposta,
  contexto, transições, execução de teste falho, classificação legada (incluindo
  atores/permissões), MCP, recursos, linhagem e catálogo. Última execução: 14,21 s
  reportados pelo pytest.
- **42 testes Community passaram**: cliente MCP real isolado, overlay efetivo,
  paginação/hashes, host e publicação/rollback no cold start.
- **364 testes na regressão selecionada**, sem representar regressão completa de
  todo o produto. Incluem um teste que grava resultado `failed` pela API do
  serviço de cenários, conclui o Test Card e relê card/spec para confirmar que
  o cenário continua falho e a Spec não foi aprovada. A autenticação Evidence V2
  utiliza um verifier de fixture isolado; não houve execução de produto externo.
- Ruff dos arquivos Python alterados e `git diff --check` sem erros. Nenhum
  board real, storage instalado ou processo em uso foi usado como fixture.

Testes principais novos/estendidos:

- [Contratos das instruções](../../okto-pulse-core-kg5-codex/tests/test_agent_instruction_contracts.py).
- [Conclusão com evidência falha preservada](../../okto-pulse-core-kg5-codex/tests/test_evidence_v2_gate_integration.py).
- [Cliente MCP e contratos comuns Community](../tests/test_mcp_resource_effective_overlay_sprint_b.py).
