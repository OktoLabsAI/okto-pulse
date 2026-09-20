# Leitura de arquivo histórico

O runtime oferece leitura por seção de uma origem arquivada:

```text
GET /api/v1/boards/{board_id}/historical-archives/{origin_kind}/{origin_id}/{section}
```

`origin_kind` e `origin_id` são proveniência opaca do arquivo. As seções aceitas
são `content`, `qa`, `evaluations` e `history`. A resposta usa o formato
`historical-archive-section/v1`, com `board_id`, `origin`, `section`, `archive_id`,
`records` e `next_offset`. `next_offset=null` encerra a paginação. O conteúdo é
somente leitura e não representa uma nova aprovação ou estado operacional.

Cada request exige identidade autenticada, acesso atual ao Board e um grant
vigente para aquela identidade, origem e seção. A autoridade capturada limita
esse grant. Revogar conteúdo bloqueia também as demais seções; revogar uma seção
não altera o arquivo original. Agentes inativos, em revisão de permissões ou sem
o vínculo atual com o Board não conseguem ler. Não há concessão automática por
nome de papel ou por possuir acesso ao Board.

Os parâmetros `offset` (padrão 0) e `limit` (padrão 100, máximo 200) paginam os
registros. A origem imutável permite continuar a leitura com o offset recebido;
a autorização é novamente consultada em cada request. Uma leitura em andamento
usa um único snapshot de ACL, grants e referência do arquivo. A próxima leitura
observa revogações já commitadas.

O servidor verifica a referência commitada, tamanho e hash do arquivo antes da
projeção. A resposta contém somente a seção solicitada da origem solicitada:
conteúdo não inclui avaliações, IDs de cenários/Business Rules, Cards, jobs,
manifest de permissões ou caminho físico do arquivo. Q&A e histórico preservam
texto, autoria e dados históricos originais, inclusive perguntas sem resposta.
Referências a outros objetos no conteúdo permanecem proveniência; não concedem
acesso ao conteúdo desses objetos.

Respostas usam `Cache-Control: no-store`. Ausência e negação retornam o mesmo
404. Fonte corrompida, indisponível ou incompatível retorna 503 sem detalhes
internos. Limites excedidos retornam 413 sem página parcial: até 100 mil registros
por seção, até 25 MiB de registros por página e arquivo de origem de até 64 MiB.
Pedidos inválidos retornam 422.

A implementação atual projeta arquivos v4 de origens aposentadas de Sprint sem
consultar suas tabelas vivas. Formatos anteriores continuam verificáveis pelo
mecanismo de migração, mas não recebem grants inventados pelo leitor. O bootstrap
normal não instala grants: essa publicação pertence ao coordenador de migração.

A administração pública dos grants, descoberta/listagem, MCP, UI histórica e
leitura das seções sujeitas a permissões próprias de Card/Spec fazem parte da
integração restante. Esta rota não executa migração, restauração ou manutenção.
