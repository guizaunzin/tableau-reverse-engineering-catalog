# Guia rápido do MVP analítico

> **Experiência independente:** esta camada analítica não faz parte do pipeline
> da Semantic Knowledge Base e não consome o modelo source schema v2.

## 1. Preparar o ambiente

```bash
python3 -m venv .venv-analytics
source .venv-analytics/bin/activate
pip install -r requirements-analytics.txt
```

## 2. Executar o exemplo público

```bash
python analytics_core.py create-example \
  --config analytics_config.example.json \
  --force
python analytics_core.py query \
  --config analytics_config.example.json \
  --request request.example.json
```

O resultado deve mostrar pedidos distintos agrupados por origem.

## 3. Extrair o workbook real

```bash
python tableau_doc.py /caminho/workbook.twbx \
  --output docs \
  --emit-json
```

Use o Markdown gerado em `docs/` para escolher uma worksheet simples.

## 4. Escolher o primeiro caso

Escolha uma worksheet com:

- uma métrica simples;
- uma dimensão;
- filtros conhecidos;
- sem LOD ou table calculation.

## 5. Criar a configuração privada

```bash
cp analytics_config.example.json analytics_config.json
```

Altere apenas:

- workbook e worksheet;
- `source.path`;
- colunas físicas;
- agregação;
- filtros permitidos e obrigatórios.

## 6. Verificar o Parquet

```bash
python analytics_core.py check --config analytics_config.json
```

O resultado mostra os ficheiros, as colunas encontradas e eventuais mappings em falta.

## 7. Criar o primeiro pedido

```bash
cp request.example.json request.json
```

Use somente os nomes semânticos definidos em `analytics_config.json`.

## 8. Executar a consulta

```bash
python analytics_core.py query \
  --config analytics_config.json \
  --request request.json
```

Guarde o SQL, os parâmetros e o resultado apresentados.

## 9. Comparar com o Tableau

Execute a mesma combinação de métrica, dimensão e filtros no Tableau e compare os valores.

## 10. Guardar os casos validados

```bash
cp validation_cases.example.json validation_cases.json
```

Substitua os casos de exemplo pelos pedidos e resultados esperados do Tableau.

## 11. Validar todos os casos

```bash
python analytics_core.py validate \
  --config analytics_config.json \
  --cases validation_cases.json
```

Cada caso será apresentado como `OK` ou `FAIL`.

## 12. Expandir devagar

Chegue primeiro a 5–10 casos `OK`. Depois adicione uma métrica, dimensão ou filtro de cada vez.
