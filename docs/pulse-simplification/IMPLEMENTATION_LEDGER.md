# Ledger de implementação — pacote Pulse v1.3

## Retomada

- Atualizado em: 2026-09-19 (America/Sao_Paulo).
- Estado: leitura obrigatória e investigação inicial; implementação do produto ainda não iniciada.
- Especificação: `D:/Projetos/Techridy/PULSE_REFACTOR/Pulse_Plano_Codex_v1_3_Arquitetura_Verificabilidade`.
- Entrada: `INICIAR_NO_CODEX_ENTREGA.md` (lido integralmente).
- Próximo passo: terminar a leitura integral do plano-base a partir da linha 446, depois KG, entrega incremental e arquitetura/verificabilidade, nessa ordem. Ler também fontes locais referenciadas e registrar lacunas antes de implementar.
- Este é o ledger da iniciativa de engenharia solicitado pelo usuário, não um segundo ledger de evidências do produto.

## Autoridade e limites

O pacote inteiro é uma especificação consolidada. O plano-base e KG continuam normativos; entrega incremental substitui somente os mecanismos delimitados de execução/impacto/entrega; arquitetura/verificabilidade acrescenta os deltas explícitos da seção 12. Não usar a ordem de leitura como substituição integral. Investigar divergências de código por entrada → autorização → lock → serviço → persistência → evento → projeção → consumidor antes de mudar semântica, autoridade, gates ou histórico.

Autorizado: código/testes em checkout isolado e dados descartáveis. Não autorizado: publicação, merge, mudanças de contas/permissões reais, parada do Pulse ativo, migração/apagamento de dados reais. Preservar alterações do usuário. Não anunciar bugs de runtime F09/F11 sem reprodução.

## Baseline observado

| Papel | Caminho | Branch | HEAD | Estado inicial |
|---|---|---|---|---|
| Checkout aberto Community | `D:/Projetos/Techridy/okto-pulse` | `main` | `3d608bf6367b9a5062d10c2046b2933323987c7f` | limpo antes deste ledger |
| Checkout irmão Core | `D:/Projetos/Techridy/okto-pulse-core` | `main` | `f0372b91228ba3e6cda8312981ec37903d11a2cd` | limpo |
| Worktree da baseline Community v1.3 | `D:/Projetos/Techridy/.worktrees/pulse-v0.3.4-kg-gaps-20260916` | `feature/v0.3.4` | `b6dda64f512920fa4aaaf9d50c331b1796b87e27` | limpo |
| Worktree da baseline Core v1.3 | `D:/Projetos/Techridy/.worktrees/pulse-core-v0.3.4-kg-gaps-20260916` | `feature/v0.3.4` | `207072509a282e8adec1481d05aee2d54c382bde` | limpo |

Os checkouts abertos são anteriores à baseline dos complementos. As revisões exatas existem localmente. Ainda não foram criados worktrees de implementação nem alteradas branches. O plano-base exige par isolado de repositórios irmãos para implementação e testes.

## Leituras e integridade

| Documento | Estado |
|---|---|
| `INICIAR_NO_CODEX_ENTREGA.md` | integral, 86 linhas |
| `README.md` do pacote | integral, 27 linhas |
| `FONTES_E_BASELINE.json` | integral |
| `PLANO_PULSE_CODEX.md` | linhas 1–445 de 845 lidas sem truncamento; leitura inicial integral truncou e não conta como concluída |
| `COMPLEMENTO_KG_RASTREABILIDADE.md` | pendente, 769 linhas |
| `COMPLEMENTO_ENTREGA_INCREMENTAL.md` | pendente, 779 linhas |
| `ESPECIFICACAO_ARQUITETURA_VERIFICABILIDADE.md` | pendente, 881 linhas |
| `IDEACAO_KG_ORIGINAL.md` | referenciado pelo README e fontes, ausente na pasta; busca de nomes sob `PULSE_REFACTOR` sem resultado |
| `MANIFEST_SHA256.json` | referenciado pelo README, ausente na pasta; busca de nomes sob `PULSE_REFACTOR` sem resultado |
| `CONTRIBUTING.md` e `CLAUDE.md`, ambos os checkouts main | lidos integralmente; claims antigos de testes/CI ainda não validados |
| `AGENTS.md` | não encontrado nos ancestrais consultados, checkouts abertos ou worktrees baseline |

## Mapa integrado de dependências (preliminar)

1. Caracterização conjunta: fase 0 do plano-base + I0 de entrega incremental + P0 de arquitetura/verificabilidade; par de repos fixo, gates/autoridades, writers/consumidores, reprodução F09/F11.
2. Contratos arquiteturais efetivos/classificação + perfis, critérios, vínculos e herança → resolução relacional única de obrigações diretas/herdadas.
3. Essa resolução alimenta planejamento, contexto do card, gate de início, admissão da evidência incremental e rollup de fechamento.
4. Registro canônico e lote atômico de Delivery Evidence → retomada e fechamento; recibos técnicos conservam validadores/autoridades de origem.
5. Autoridade relacional e decisões → projeções KG automáticas e consumidores; projeção atrasada não decide gates.
6. Migrações Sprint/KG/Delivery/verificabilidade devem preservar IDs, ACLs, vínculos legítimos, policies e histórico; distribuir Core/Community compatíveis.
7. UI, REST/MCP, instruções, pacote instalado, testes e medição do fluxo dependem dos contratos conjuntos; não implementar duas associações ou fontes de aprovação.

Mapa preliminar derivado da entrada: completar após leitura integral dos quatro documentos. Nenhum contrato compartilhado foi definido ainda.

## Decisões / investigações

- D001: usar a baseline v0.3.4 local para caracterização; não tratar ausência de funcionalidades em main/v0.3.3 como defeito da baseline v1.3.
- Q001: localizar fontes ausentes (`IDEACAO_KG_ORIGINAL.md`, `MANIFEST_SHA256.json`) antes de declarar pacote inteiramente lido/verificado. Não sintetizar seu conteúdo nem substituir por hipótese.
- Q002: validar que instruções/manifests da baseline não divergem dos checkouts main já lidos.

## Verificação / alterações

- Executado: `git status --short`, HEAD/branch e inventário de worktrees em ambos os repositórios; listagem de documentos; busca de instruções e arquivos ausentes.
- Testes de produto, persistência descartável, build, migração, benchmarks: não executados.
- Arquivo criado: este ledger. Código, dados, permissões e processos do produto: não alterados.
- Critérios concluídos: nenhum critério funcional declarado concluído.

## Diário

- 2026-09-19: iniciada leitura pela instrução de entrada; registrada divergência main/v0.3.3 versus baseline feature/v0.3.4 e localizadas revisões exatas em worktrees limpos. Identificadas referências a dois arquivos não presentes na pasta entregue. Criado ledger antes de qualquer implementação para retomada segura.
