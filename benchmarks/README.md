# Benchmarks e Investigación

Esta carpeta contiene benchmarks y artefactos de comparación usados durante el proceso de evaluación técnica del proyecto.

## Estado actual

El stack principal del producto es **LangGraph**.

Los scripts de esta carpeta asociados a `Vercel AI SDK` se conservan solo como referencia histórica y de investigación comparativa.

## Archivos

- **`vercel-ai-sdk-compare.mjs`**
  - benchmark base de comparación

- **`vercel-ai-sdk-embudo-agent.mjs`**
  - benchmark del agente de embudo con contexto real

- **`vercel-ai-sdk-parallel-agents.mjs`**
  - benchmark de dos agentes paralelos con tools async

- **`vercel-ai-sdk-documented-real-flow.mjs`**
  - corrida documentada con prompts, tools, respuestas y contexto real

## Fuente principal del benchmark final

Para el benchmark final documentado revisar:

- **`docs/BENCHMARK_REAL_FLOW_RESULTS.md`**
- **`artifacts/benchmark_documented_langgraph.json`**
- **`artifacts/benchmark_documented_vercel.json`**

## Nota

Si en el futuro estos benchmarks dejan de ser útiles, esta carpeta puede moverse a una zona de `archive/` o `research/` sin afectar el runtime principal del sistema.
