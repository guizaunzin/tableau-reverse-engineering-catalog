# MCP Server: guia de implementação e adoção

## Resumo executivo

O MCP Server transforma duas fontes controladas em ferramentas que um LLM pode
consultar:

1. o catálogo extraído dos workbooks Tableau, que descreve fórmulas,
   dependências, worksheets, Dashboards e Metric Contracts;
2. uma configuração semântica aprovada, que mapeia nomes de negócio para
   tabelas e colunas físicas, descrições e políticas de agregação.

O objetivo não é permitir que o LLM escreva SQL livremente. O servidor só gera
SQL depois de confirmar que datasource, dimensões, indicadores e agregações
pertencem à configuração aprovada.

## O que foi construído

O servidor expõe oito ferramentas somente leitura:

| Ferramenta | Finalidade |
|---|---|
| `search_catalog` | Encontrar workbooks, worksheets e fields do Tableau. |
| `get_field_impact` | Consultar dependências e impacto de um field. |
| `get_worksheet` | Consultar filtros, cálculos e fields de uma worksheet. |
| `get_metric_contract` | Obter a receita semântica extraída de uma métrica Tableau. |
| `trace_dependencies` | Percorrer dependências upstream ou downstream. |
| `get_dimensions` | Listar dimensões autorizadas com coluna e descrição. |
| `get_indicators` | Listar indicadores, descrições e políticas de agregação. |
| `get_data` | Validar uma solicitação, gerar SQL seguro e devolver resultados em JSON. |

### `get_dimensions()`

Recebe um datasource configurado e devolve as dimensões disponíveis, incluindo
nome de negócio, coluna física e descrição.

### `get_indicators()`

Devolve indicadores, descrições, agregação padrão e agregações permitidas. Por
exemplo, `Revenue` pode usar `sum` por padrão e permitir `avg`, enquanto
`Orders` pode aceitar apenas `count_distinct`.

### `get_data()`

Recebe datasource, dimensões, indicadores, overrides opcionais de agregação e
um limite entre 1 e 1.000 linhas. O resultado contém SQL, parâmetros, políticas
efetivamente usadas, quantidade de linhas e dados serializáveis em JSON.

### Validação de inputs

Antes de abrir uma conexão, o servidor valida:

- existência do datasource;
- existência e ausência de duplicatas nas dimensões e indicadores;
- máximo de 10 dimensões e 20 indicadores;
- agregação solicitada contra a allowlist do indicador;
- limite de linhas;
- formato seguro dos identificadores definidos na configuração.

### Geração segura de SQL

O utilizador e o LLM nunca fornecem nomes físicos diretamente. Os nomes de
negócio são resolvidos para identificadores previamente configurados. A geração:

- aceita somente identificadores validados;
- aplica apenas `SUM`, `AVG`, `MIN`, `MAX`, `COUNT` e `COUNT DISTINCT`;
- adiciona `GROUP BY` para as dimensões selecionadas;
- usa `LIMIT ?` parametrizado;
- abre SQLite com `mode=ro`;
- não aceita fragmentos SQL, expressões, cláusulas ou filtros livres.

Uma entrada como `Region; DROP TABLE sales` não corresponde a uma dimensão
configurada e é rejeitada antes da execução.

## Como as peças se relacionam

```text
TWB/TWBX
   │
   ▼
tableau_doc.py ──► catálogo JSON ──► fórmulas, linhagem e Metric Contracts
                                           │
semantic_config.json ──► nomes aprovados ──┤
                                           ▼
                                    tableau_mcp.py
                                      │         │
                                      │         └─► get_data() ──► SQLite read-only
                                      └─► ferramentas de documentação
```

O catálogo responde “como o Tableau define e usa esta métrica?”. A configuração
semântica responde “onde estão os dados físicos aprovados e quais operações são
permitidas?”. As duas partes são complementares, mas ainda não são reconciliadas
automaticamente.

## Configuração semântica

Copie `semantic_config.example.json` para `semantic_config.json`. Não coloque
senhas, tokens ou credenciais nesse ficheiro.

```json
{
  "version": 1,
  "datasources": {
    "Commercial Metrics": {
      "description": "Approved semantic source.",
      "table": "commercial_metrics",
      "connection": {
        "driver": "sqlite",
        "database": "data/commercial_metrics.db"
      },
      "dimensions": {
        "Region": {
          "column": "region",
          "description": "Approved reporting region."
        }
      },
      "indicators": {
        "Revenue": {
          "column": "revenue",
          "description": "Gross recognized revenue.",
          "default_aggregation": "sum",
          "allowed_aggregations": ["sum", "avg"]
        }
      }
    }
  }
}
```

Os nomes `Region` e `Revenue` são o contrato apresentado ao LLM. `column` e
`table` são identificadores físicos controlados pela equipa.

## Como executar localmente

Gere primeiro o catálogo:

```bash
python3 tableau_doc.py /path/to/workbooks --output docs --emit-json
```

Instale o SDK opcional:

```bash
python3 -m venv .venv-mcp
source .venv-mcp/bin/activate
pip install -r requirements-mcp.txt
```

Valide catálogo e configuração sem iniciar o servidor:

```bash
python3 tableau_mcp.py \
  --catalog docs \
  --semantic-config semantic_config.json \
  --check
```

Inicie o servidor:

```bash
python3 tableau_mcp.py \
  --catalog docs \
  --semantic-config semantic_config.json
```

Para um cliente MCP, copie `mcp_config.example.json`, use caminhos absolutos e
reinicie o cliente.

## Fluxo esperado para o LLM

Para responder “qual foi a receita por região?”:

1. chamar `get_metric_contract` para entender a definição Tableau de
   `Revenue`;
2. chamar `get_dimensions` para confirmar que `Region` está disponível;
3. chamar `get_indicators` para confirmar `Revenue` e sua agregação padrão;
4. chamar `get_data` com `dimensions=["Region"]` e
   `indicators=["Revenue"]`;
5. explicar o resultado junto com o contrato e as limitações conhecidas.

O LLM não deve saltar diretamente para `get_data` inventando campos.

## O que fazer em seguida no trabalho

### 1. Escolher um piloto pequeno

Escolha um Dashboard importante com duas ou três métricas e números de
referência conhecidos no Tableau.

### 2. Definir owners

Para cada métrica, identifique owner de negócio, owner dos dados, datasource e
tabela aprovados, definição, agregação padrão, exceções e dimensões autorizadas.

### 3. Preencher a configuração

Mapeie apenas os campos aprovados. Uma allowlist pequena torna o comportamento
mais seguro e mais fácil de validar.

### 4. Validar contra o Tableau

Compare queries por combinações conhecidas de data e dimensão. Registre valor
Tableau, valor MCP, filtros, diferença, causa e aprovação do owner. Uma métrica
só deve ser apresentada como equivalente após essa validação.

### 5. Escolher o adapter corporativo

O executor atual suporta SQLite somente leitura. Para produção, a equipa deve
escolher Snowflake, BigQuery, SQL Server ou outro warehouse. O adapter deverá:

- usar credenciais geridas fora da configuração;
- executar com role somente leitura;
- aplicar timeout, limite de custo e limite de linhas;
- manter a mesma validação e o mesmo compilador restrito;
- registrar auditoria sem guardar dados sensíveis.

### 6. Fazer um piloto com utilizadores

Teste se o LLM escolhe a métrica correta, usa a agregação padrão, pergunta
quando falta contexto, cita limitações e reproduz os valores validados.

## Como apresentar ao seu chefe

Uma narrativa curta:

> Hoje as pessoas tratam o Tableau como golden source, mas o LLM não conhece as
> regras que produziram aqueles números. Construímos uma camada que extrai a
> definição das métricas do Tableau e expõe apenas dimensões, indicadores e
> agregações aprovadas. O LLM não escreve SQL livre: ele solicita conceitos de
> negócio, o servidor valida esses conceitos e gera uma query limitada e
> somente leitura. O próximo passo é validar um pequeno grupo de métricas contra
> um Dashboard real e conectar o mecanismo ao nosso warehouse corporativo.

Para uma demonstração:

1. mostre `get_metric_contract("Revenue")`;
2. mostre `get_indicators()` e a política `sum`;
3. tente uma dimensão inválida e mostre a rejeição;
4. execute uma consulta válida por `Region`;
5. compare o resultado com um valor conhecido do Tableau.

## Estado atual e limitações

- Metric Contracts não capturam ainda todo o modelo físico, joins ou a ordem
  completa de operações do Tableau.
- O executor suporta SQLite somente leitura.
- Não existem filtros livres em `get_data`; isso é intencional no primeiro
  núcleo seguro.
- Não há ainda autenticação corporativa, auditoria persistente, timeout ou
  controlo de custo.
- A equivalência com o Tableau precisa ser validada métrica por métrica.

Esses pontos não impedem um piloto. Eles definem a diferença entre um protótipo
controlado e um serviço de produção.
