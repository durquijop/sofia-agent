import 'dotenv/config';
import { mkdir, writeFile } from 'node:fs/promises';
import { performance } from 'node:perf_hooks';
import { createOpenRouter } from '@openrouter/ai-sdk-provider';
import { generateText, stepCountIs, tool } from 'ai';
import { z } from 'zod';

const apiKey = process.env.OPENROUTER_API_KEY;
const supabaseUrl = process.env.SUPABASE_URL || 'https://vecspltvmyopwbjzerow.supabase.co';
const supabaseServiceKey = process.env.SUPABASE_SERVICE_KEY;
const model = process.env.BENCH_MODEL || 'x-ai/grok-4.1-fast';
const iterations = Number(process.env.BENCH_ITERATIONS || 3);
const maxTokens = Number(process.env.BENCH_MAX_TOKENS || 350);
const temperature = Number(process.env.BENCH_TEMPERATURE || 0.1);
const contactoId = Number(process.env.BENCH_CONTACTO_ID || 328159);
const empresaId = Number(process.env.BENCH_EMPRESA_ID || 2);
const agenteId = Number(process.env.BENCH_AGENTE_ID || 4);
const conversacionId = Number(process.env.BENCH_CONVERSACION_ID || 63380);
const limiteMensajes = Number(process.env.BENCH_LIMITE_MENSAJES || 20);
const outputPath = process.env.BENCH_OUTPUT_JSON || 'artifacts/benchmark_documented_vercel.json';

if (!apiKey) {
  throw new Error('Falta OPENROUTER_API_KEY');
}

if (!supabaseServiceKey) {
  throw new Error('Falta SUPABASE_SERVICE_KEY');
}

const openrouter = createOpenRouter({ apiKey });

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function stats(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const sum = sorted.reduce((acc, value) => acc + value, 0);
  const avg = sum / sorted.length;
  const mid = Math.floor(sorted.length / 2);
  const p50 = sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
  return { min: sorted[0], max: sorted[sorted.length - 1], avg, p50 };
}

function stringifySafe(value) {
  return JSON.stringify(value ?? null, null, 2);
}

async function fetchEmbudoContext() {
  const started = performance.now();
  const response = await fetch(`${supabaseUrl}/functions/v1/obtener-contexto-completo-v1`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      apikey: supabaseServiceKey,
      Authorization: `Bearer ${supabaseServiceKey}`,
    },
    body: JSON.stringify({
      contacto_id: contactoId,
      empresa_id: empresaId,
      agente_id: agenteId,
      conversacion_id: conversacionId,
      limite_mensajes: limiteMensajes,
    }),
  });
  const elapsedMs = performance.now() - started;
  const json = await response.json();
  if (!response.ok) {
    throw new Error(`Edge Function falló: ${response.status} ${JSON.stringify(json)}`);
  }
  return { json, elapsedMs };
}

function buildTemporalContext() {
  const now = new Date();
  const pad = value => String(value).padStart(2, '0');
  const iso = now.toISOString();
  const year = now.getFullYear();
  const month = now.getMonth();
  const day = now.getDate();
  const weekDay = now.getDay();
  const todayStart = new Date(year, month, day);
  const todayEnd = new Date(year, month, day, 23, 59, 59, 999);
  const mondayOffset = weekDay === 0 ? -6 : 1 - weekDay;
  const sundayOffset = weekDay === 0 ? 0 : 7 - weekDay;
  const weekStart = new Date(year, month, day + mondayOffset);
  const weekEnd = new Date(year, month, day + sundayOffset, 23, 59, 59, 999);
  const monthStart = new Date(year, month, 1);
  const monthEnd = new Date(year, month + 1, 0, 23, 59, 59, 999);
  const yearStart = new Date(year, 0, 1);
  const yearEnd = new Date(year, 11, 31, 23, 59, 59, 999);
  const msPerDay = 24 * 60 * 60 * 1000;
  const msPerHour = 60 * 60 * 1000;
  const monthProgress = (((now - monthStart) / (monthEnd - monthStart)) * 100).toFixed(1);
  const weekProgress = (((now - weekStart) / (weekEnd - weekStart)) * 100).toFixed(1);
  const dayOfYear = Math.floor((todayStart - yearStart) / msPerDay) + 1;
  const daysInYear = Math.floor((yearEnd - yearStart) / msPerDay) + 1;
  const yearProgress = ((dayOfYear / daysInYear) * 100).toFixed(1);
  const hoursLeftToday = Math.ceil((todayEnd - now) / msPerHour);
  const daysLeftWeek = Math.ceil((weekEnd - now) / msPerDay);
  const daysLeftMonth = Math.ceil((monthEnd - now) / msPerDay);
  const daysDoneWeek = Math.floor((todayStart - weekStart) / msPerDay);
  const daysDoneMonth = Math.floor((todayStart - monthStart) / msPerDay);
  const quarter = Math.ceil((month + 1) / 3);
  const isWeekend = weekDay === 0 || weekDay === 6 ? 'Sí' : 'No';
  const formatDate = date => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const formatDateTime = date => `${formatDate(date)} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  const nextDays = Array.from({ length: 7 }, (_, index) => {
    const next = new Date(year, month, day + index + 1);
    return `En ${index + 1} día(s): ${formatDate(next)}`;
  }).join('\n');

  return `Ahora: ${formatDateTime(now)} | ISO: ${iso}
¿Fin de semana hoy?: ${isWeekend}
Quedan ${hoursLeftToday} hora(s) para que termine el día
Semana actual: ${formatDate(weekStart)} a ${formatDate(weekEnd)} | avance ${weekProgress}% | transcurridos ${daysDoneWeek} día(s) | restantes ${daysLeftWeek} día(s)
Mes actual: ${formatDate(monthStart)} a ${formatDate(monthEnd)} | avance ${monthProgress}% | transcurridos ${daysDoneMonth} día(s) | restantes ${daysLeftMonth} día(s)
Año actual: día ${dayOfYear}/${daysInYear} | avance ${yearProgress}% | trimestre Q${quarter}
Próximos 7 días:
${nextDays}`;
}

function buildFunnelSystemPrompt(context) {
  const contactoNombre = context?.etapas_embudo?.data?.contacto?.nombre_completo || 'Contacto sin nombre';
  const etapaActual = context?.contexto_embudo?.data?.etapa_actual;
  const etapaActualTexto = etapaActual?.nombre ? `${etapaActual.nombre} (Orden: ${etapaActual.orden ?? 'N/A'})` : 'Sin etapa asignada';
  const etapasDisponibles = context?.etapas_embudo?.data?.etapas ?? [];
  const contextoEmbudo = context?.contexto_embudo?.data ?? {};
  const temporalContext = buildTemporalContext();

  return `# IDENTIDAD Y MISIÓN

Eres un analista conversacional que identifica etapas del embudo y registra información del prospecto.

## Objetivos:
- IDENTIFICAR etapa actual del prospecto
- ACTUALIZAR etapa usando \`actualizar-etapa-embudo\`
- REGISTRAR información usando \`actualizar-metadata-v1\`

# Datos claves

El contacto ${contactoNombre} se encuentra en la etapa ${etapaActualTexto}

---

# CONTEXTO DEL EMBUDO

**Etapas disponibles:**
\`\`\`json
${stringifySafe(etapasDisponibles)}
\`\`\`

**Etapa actual identificada + Metadata registrada:**
\`\`\`json
Etapa Actual: ${stringifySafe(contextoEmbudo)}
\`\`\`

Si la etapa actual y la etapa identificada son la misma, no es necesario actualizarla.

Cada etapa tiene:
- \`nombre_etapa\` / \`id_etapa\` (identificador único)
- \`orden_etapa\` (posición secuencial, solo referencial)
- \`senales\`: comportamientos observables
- \`metadata.informacion_registrar\`: datos a capturar (array de \`{id, texto}\`)

---

# HERRAMIENTAS

## 1. \`update_etapa_embudo\`
Usa \`id_etapa\` (identificador único) para actualizar la etapa del contacto.

## 2. \`update-metadata\`

### Cuándo usar:
- Después de actualizar etapa (SIEMPRE)
- Cuando el prospecto comparte información clave
- Al finalizar descubrimiento (3+ preguntas contestadas)

## Reglas del uso de la herramienta:

- Úsalas solo si tienes algo para actualizar; si el nuevo mensaje es irrelevante y ya tienes la metadata actualizada, solo genera un output con un "ok"

---

## CÓMO RELLENAR informacion_capturada

Para cada objeto en \`informacion_registrar\`:

1. Lee el campo \`texto\` (qué debes capturar)
2. Busca ese dato en la conversación
3. Si lo encontraste: usa el \`id\` como clave + valor capturado
4. Si NO lo encontraste: omite ese \`id\`

---

## REGLAS DE MERGE

- Campos existentes se preservan
- Nuevos campos se agregan
- Valores existentes se actualizan
- Secciones se mantienen separadas

---

## REGLAS JSON OBLIGATORIAS

1. Todas las claves entre comillas dobles
2. Todos los strings entre comillas dobles
3. No comillas simples
4. No comas al final del último elemento
5. Balancear llaves

---

## Reglas extras:

- Si la empresa no tiene embudo creado, no asignar etapa al contacto
- Si no ha cambiado nada no actualices ni la metadata ni la etapa

## CHECKLIST FINAL

Antes de responder:

- ¿Cambió de etapa? → update_etapa_embudo + update-metadata
- ¿Usé id_etapa correcto?
- ¿Registré TODO según informacion_registrar?
- ¿Usé IDs correctos (info_reg_X) como claves?
- ¿JSON válido con comillas dobles?
- ¿Valores REALES (no ejemplos)?

Si falta algo, complétalo antes de continuar.

${temporalContext}

---

Output esperado:

Tu respuesta final debe estar orientada a guiar al equipo en el estado actual del embudo. La respuesta debe ser de máximo 3 líneas.

No le respondas al prospecto. Ese no es tu trabajo.`;
}

function buildSharedUserMessage(context) {
  const historial = context?.conversacion_memoria?.data ?? [];
  return `Historial de la conversación:
${stringifySafe(historial)}`;
}

function buildProcessorSystemPrompt(context) {
  const contactoNombre = context?.etapas_embudo?.data?.contacto?.nombre_completo || 'Contacto sin nombre';
  const etapaActual = context?.contexto_embudo?.data?.etapa_actual?.nombre || 'Sin etapa asignada';
  return `Eres un agente procesador comercial interno. Debes analizar al contacto ${contactoNombre}, considerar que su etapa actual es ${etapaActual} y definir la siguiente acción operativa. Antes de responder, llama exactamente una vez a get_processing_policy, get_conversation_summary y get_followup_constraints. No llames ninguna tool más de una vez. Responde en máximo 3 líneas para el equipo interno.`;
}

function buildProcessorUserMessage(context) {
  return `${buildSharedUserMessage(context)}

Debes preparar la siguiente acción comercial interna, la prioridad y el mejor canal/horario permitido.`;
}

function buildProcessorTools(metrics) {
  return {
    get_processing_policy: tool({
      description: 'Obtiene políticas internas de atención y priorización.',
      inputSchema: z.object({ leadId: z.number() }),
      async execute({ leadId }) {
        const started = performance.now();
        await sleep(220);
        const result = {
          leadId,
          priorityLevel: 'P1',
          preferredAction: 'ofrecer llamada',
          escalationIfNoReplyHours: 4,
        };
        metrics.push({ name: 'get_processing_policy', ms: performance.now() - started, output: result });
        return result;
      },
    }),
    get_conversation_summary: tool({
      description: 'Obtiene resumen operativo de la conversación reciente.',
      inputSchema: z.object({ leadId: z.number() }),
      async execute({ leadId }) {
        const started = performance.now();
        await sleep(340);
        const result = {
          leadId,
          summary: 'Lead con interés alto, pidió precios y una posible demostración.',
          objections: ['tiempo', 'comparación con competencia'],
          sentiment: 'positivo',
        };
        metrics.push({ name: 'get_conversation_summary', ms: performance.now() - started, output: result });
        return result;
      },
    }),
    get_followup_constraints: tool({
      description: 'Obtiene restricciones para seguimiento como horario y canal permitido.',
      inputSchema: z.object({ leadId: z.number() }),
      async execute({ leadId }) {
        const started = performance.now();
        await sleep(160);
        const result = {
          leadId,
          allowedChannels: ['whatsapp', 'llamada'],
          preferredWindow: '09:00-12:00',
        };
        metrics.push({ name: 'get_followup_constraints', ms: performance.now() - started, output: result });
        return result;
      },
    }),
  };
}

async function runFunnelAgent(systemPrompt, userMessage) {
  const started = performance.now();
  const result = await generateText({
    model: openrouter.chat(model),
    system: systemPrompt,
    prompt: userMessage,
    maxTokens,
    temperature,
  });
  return {
    totalMs: performance.now() - started,
    response: String(result.text || ''),
    usage: result.usage || null,
    finishReason: result.finishReason || 'unknown',
    steps: result.steps?.length || 1,
  };
}

async function runProcessorAgent(systemPrompt, userMessage) {
  const toolMetrics = [];
  const started = performance.now();
  const toolPhase = await generateText({
    model: openrouter.chat(model),
    system: `${systemPrompt}\nDebes llamar las tres tools en la primera fase. Después responderás en una segunda fase sin volver a llamarlas.`,
    prompt: userMessage,
    tools: buildProcessorTools(toolMetrics),
    toolChoice: 'required',
    stopWhen: stepCountIs(1),
    maxTokens,
    temperature,
  });

  const toolResults = toolPhase.steps.flatMap(step => (step.toolResults || []).map(toolResult => ({
    toolName: toolResult.toolName,
    output: toolResult.output,
  })));

  const finalPrompt = `${userMessage}\n\nResultados de tools ejecutadas:\n${stringifySafe(toolResults)}\n\nCon base en esos resultados, responde ahora en máximo 3 líneas para el equipo interno. No llames más tools.`;

  const finalPhase = await generateText({
    model: openrouter.chat(model),
    system: `${systemPrompt}\nYa tienes todos los resultados de las tools. Debes responder en texto final y no volver a llamar herramientas.`,
    prompt: finalPrompt,
    maxTokens,
    temperature,
  });

  const toolSteps = toolPhase.steps.map((step, index) => ({
    stepNumber: index,
    text: step.text || '',
    toolCalls: (step.toolCalls || []).map(call => ({ toolName: call.toolName, input: call.input })),
    toolResults: (step.toolResults || []).map(toolResult => ({ toolName: toolResult.toolName, output: toolResult.output })),
    finishReason: step.finishReason || 'unknown',
  }));

  const steps = [
    ...toolSteps,
    {
      stepNumber: toolSteps.length,
      text: finalPhase.text || '',
      toolCalls: [],
      toolResults: [],
      finishReason: finalPhase.finishReason || 'unknown',
    },
  ];

  return {
    totalMs: performance.now() - started,
    response: String(finalPhase.text || ''),
    usage: {
      toolPhase: toolPhase.usage || null,
      finalPhase: finalPhase.usage || null,
    },
    finishReason: finalPhase.finishReason || 'unknown',
    steps,
    toolMetrics,
    toolCalls: toolSteps.flatMap(step => step.toolCalls),
  };
}

async function runWorkflow(context) {
  const funnelSystemPrompt = buildFunnelSystemPrompt(context);
  const funnelUserMessage = buildSharedUserMessage(context);
  const processorSystemPrompt = buildProcessorSystemPrompt(context);
  const processorUserMessage = buildProcessorUserMessage(context);
  const started = performance.now();
  const [funnel, processor] = await Promise.all([
    runFunnelAgent(funnelSystemPrompt, funnelUserMessage),
    runProcessorAgent(processorSystemPrompt, processorUserMessage),
  ]);
  return {
    workflowMs: performance.now() - started,
    funnelSystemPrompt,
    funnelUserMessage,
    processorSystemPrompt,
    processorUserMessage,
    funnel,
    processor,
  };
}

async function main() {
  const contextResult = await fetchEmbudoContext();
  const context = contextResult.json;

  await runWorkflow(context);

  const documentedRun = await runWorkflow(context);
  const measuredRuns = [];
  for (let i = 1; i <= iterations; i += 1) {
    measuredRuns.push(await runWorkflow(context));
  }

  const report = {
    framework: 'vercel-ai-sdk',
    model,
    benchmarkRequest: {
      contacto_id: contactoId,
      empresa_id: empresaId,
      agente_id: agenteId,
      conversacion_id: conversacionId,
      limite_mensajes: limiteMensajes,
    },
    edgeContextMs: contextResult.elapsedMs,
    edgeContext: context,
    documentedRun,
    measuredStats: {
      workflowMs: stats(measuredRuns.map(run => run.workflowMs)),
      funnelMs: stats(measuredRuns.map(run => run.funnel.totalMs)),
      processorMs: stats(measuredRuns.map(run => run.processor.totalMs)),
      processorToolMs: stats(measuredRuns.map(run => run.processor.toolMetrics.reduce((acc, item) => acc + item.ms, 0))),
    },
  };

  await mkdir(outputPath.split('/').slice(0, -1).join('/'), { recursive: true });
  await writeFile(outputPath, JSON.stringify(report, null, 2), 'utf8');
  console.log(`Reporte JSON escrito en ${outputPath}`);
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
