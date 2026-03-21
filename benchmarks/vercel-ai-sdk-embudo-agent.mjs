import 'dotenv/config';
import { performance } from 'node:perf_hooks';
import { generateText } from 'ai';
import { createOpenRouter } from '@openrouter/ai-sdk-provider';

const openRouterApiKey = process.env.OPENROUTER_API_KEY;
const supabaseUrl = process.env.SUPABASE_URL || 'https://vecspltvmyopwbjzerow.supabase.co';
const supabaseServiceKey = process.env.SUPABASE_SERVICE_KEY;
const baseUrl = process.env.BENCH_FASTAPI_URL || 'http://localhost:8080';
const model = process.env.BENCH_MODEL || 'x-ai/grok-4.1-fast';
const iterations = Number(process.env.BENCH_ITERATIONS || 3);
const maxTokens = Number(process.env.BENCH_MAX_TOKENS || 350);
const temperature = Number(process.env.BENCH_TEMPERATURE || 0.1);
const contactoId = Number(process.env.BENCH_CONTACTO_ID || 328159);
const empresaId = Number(process.env.BENCH_EMPRESA_ID || 2);
const agenteId = Number(process.env.BENCH_AGENTE_ID || 4);
const conversacionId = Number(process.env.BENCH_CONVERSACION_ID || 63380);
const limiteMensajes = Number(process.env.BENCH_LIMITE_MENSAJES || 20);

if (!openRouterApiKey) {
  throw new Error('Falta OPENROUTER_API_KEY');
}

if (!supabaseServiceKey) {
  throw new Error('Falta SUPABASE_SERVICE_KEY');
}

const openrouter = createOpenRouter({ apiKey: openRouterApiKey });

function formatMs(value) {
  return `${value.toFixed(1)}ms`;
}

function stats(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const sum = sorted.reduce((acc, value) => acc + value, 0);
  const avg = sum / sorted.length;
  const mid = Math.floor(sorted.length / 2);
  const p50 = sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
  return {
    min: sorted[0],
    max: sorted[sorted.length - 1],
    avg,
    p50,
  };
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

function stringifySafe(value) {
  return JSON.stringify(value ?? null, null, 2);
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

  return `// ============================================
// CONTEXTO TEMPORAL COMPLETO
// ============================================
Ahora: ${formatDateTime(now)} | ISO: ${iso}
¿Fin de semana hoy?: ${isWeekend}
Quedan ${hoursLeftToday} hora(s) para que termine el día

// SEMANA ACTUAL
Rango: ${formatDate(weekStart)} a ${formatDate(weekEnd)}
Avance: ${weekProgress}% | Transcurridos: ${daysDoneWeek} día(s) | Restantes: ${daysLeftWeek} día(s)

// MES ACTUAL
Rango: ${formatDate(monthStart)} a ${formatDate(monthEnd)}
Avance: ${monthProgress}% | Transcurridos: ${daysDoneMonth} día(s) | Restantes: ${daysLeftMonth} día(s)

// AÑO ACTUAL
Día del año: ${dayOfYear}/${daysInYear} | Avance: ${yearProgress}% | Trimestre: Q${quarter}

// PRÓXIMOS 7 DÍAS
${nextDays}`;
}

function buildSystemPrompt(context) {
  const contactoNombre = context?.etapas_embudo?.data?.contacto?.nombre_completo || 'Contacto sin nombre';
  const etapaActual = context?.contexto_embudo?.data?.etapa_actual;
  const etapaActualTexto = etapaActual?.nombre
    ? `${etapaActual.nombre} (Orden: ${etapaActual.orden ?? 'N/A'})`
    : 'Sin etapa asignada';
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

function buildUserMessage(context) {
  const historial = context?.conversacion_memoria?.data ?? [];
  return `Historial de la conversación:
${stringifySafe(historial)}

Usa las tools si es necesario.`;
}

function buildRunUserMessage(baseUserMessage, runLabel) {
  return `${baseUserMessage}

[benchmark_run_id: ${runLabel}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}]`;
}

async function runCurrentFastApi(systemPrompt, userMessage, iteration) {
  const started = performance.now();
  const response = await fetch(`${baseUrl}/api/v1/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      system_prompt: systemPrompt,
      message: userMessage,
      model,
      mcp_servers: [],
      conversation_id: `bench-embudo-fastapi-${Date.now()}-${iteration}`,
      max_tokens: maxTokens,
      temperature,
    }),
  });

  const totalMs = performance.now() - started;
  const json = await response.json();

  if (!response.ok) {
    throw new Error(`FastAPI benchmark falló: ${response.status} ${JSON.stringify(json)}`);
  }

  return {
    roundtripMs: totalMs,
    serverTotalMs: Number(json?.timing?.total_ms || 0),
    serverLlmMs: Number(json?.timing?.llm_ms || 0),
    responseLength: String(json?.response || '').length,
  };
}

async function runVercelAiSdk(systemPrompt, userMessage) {
  const started = performance.now();
  const result = await generateText({
    model: openrouter.chat(model),
    system: systemPrompt,
    prompt: userMessage,
    maxTokens,
    temperature,
  });
  const totalMs = performance.now() - started;

  return {
    roundtripMs: totalMs,
    responseLength: String(result.text || '').length,
    finishReason: result.finishReason || 'unknown',
    usage: result.usage || null,
  };
}

async function main() {
  console.log('Benchmark agente de embudo');
  console.log(`Model: ${model}`);
  console.log(`FastAPI URL: ${baseUrl}`);
  console.log(`Contexto: contacto=${contactoId} empresa=${empresaId} agente=${agenteId} conversacion=${conversacionId}`);
  console.log('');

  const contextResult = await fetchEmbudoContext();
  const systemPrompt = buildSystemPrompt(contextResult.json);
  const baseUserMessage = buildUserMessage(contextResult.json);

  console.log(`Edge Function contexto -> ${formatMs(contextResult.elapsedMs)}`);
  console.log(`System prompt chars -> ${systemPrompt.length}`);
  console.log(`User message chars -> ${baseUserMessage.length}`);
  console.log('');

  console.log('Warm-up no medido...');
  {
    const warmupUserMessage = buildRunUserMessage(baseUserMessage, 'warmup');
    await runCurrentFastApi(systemPrompt, warmupUserMessage, 0);
    await runVercelAiSdk(systemPrompt, warmupUserMessage);
  }
  console.log('Warm-up completado');
  console.log('');

  const currentFastApi = [];
  const vercelAiSdk = [];

  for (let i = 1; i <= iterations; i += 1) {
    const runUserMessage = buildRunUserMessage(baseUserMessage, `iter-${i}`);
    const current = await runCurrentFastApi(systemPrompt, runUserMessage, i);
    const vercel = await runVercelAiSdk(systemPrompt, runUserMessage);

    currentFastApi.push(current);
    vercelAiSdk.push(vercel);

    console.log(`Iteración ${i}`);
    console.log(`  FastAPI embudo     -> roundtrip=${formatMs(current.roundtripMs)} | server_total=${formatMs(current.serverTotalMs)} | llm=${formatMs(current.serverLlmMs)} | chars=${current.responseLength}`);
    console.log(`  Vercel AI SDK      -> roundtrip=${formatMs(vercel.roundtripMs)} | finish=${vercel.finishReason} | chars=${vercel.responseLength}`);
    console.log('');
  }

  const fastRoundtrip = stats(currentFastApi.map(item => item.roundtripMs));
  const fastServer = stats(currentFastApi.map(item => item.serverTotalMs));
  const fastLlm = stats(currentFastApi.map(item => item.serverLlmMs));
  const vercelRoundtrip = stats(vercelAiSdk.map(item => item.roundtripMs));

  console.log('=== RESUMEN ===');
  console.log(`Edge Function contexto       -> ${formatMs(contextResult.elapsedMs)}`);
  console.log(`FastAPI embudo roundtrip     -> avg=${formatMs(fastRoundtrip.avg)} | p50=${formatMs(fastRoundtrip.p50)} | min=${formatMs(fastRoundtrip.min)} | max=${formatMs(fastRoundtrip.max)}`);
  console.log(`FastAPI embudo server total  -> avg=${formatMs(fastServer.avg)} | p50=${formatMs(fastServer.p50)} | min=${formatMs(fastServer.min)} | max=${formatMs(fastServer.max)}`);
  console.log(`FastAPI embudo llm only      -> avg=${formatMs(fastLlm.avg)} | p50=${formatMs(fastLlm.p50)} | min=${formatMs(fastLlm.min)} | max=${formatMs(fastLlm.max)}`);
  console.log(`Vercel AI SDK roundtrip      -> avg=${formatMs(vercelRoundtrip.avg)} | p50=${formatMs(vercelRoundtrip.p50)} | min=${formatMs(vercelRoundtrip.min)} | max=${formatMs(vercelRoundtrip.max)}`);
  console.log(`Delta avg (FastAPI - Vercel) -> ${formatMs(fastRoundtrip.avg - vercelRoundtrip.avg)}`);
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
