import 'dotenv/config';
import { performance } from 'node:perf_hooks';
import { generateText } from 'ai';
import { createOpenRouter } from '@openrouter/ai-sdk-provider';

const apiKey = process.env.OPENROUTER_API_KEY;
const baseUrl = process.env.BENCH_FASTAPI_URL || 'http://localhost:8080';
const model = process.env.BENCH_MODEL || 'x-ai/grok-4.1-fast';
const iterations = Number(process.env.BENCH_ITERATIONS || 5);
const maxTokens = Number(process.env.BENCH_MAX_TOKENS || 256);
const temperature = Number(process.env.BENCH_TEMPERATURE || 0.2);

function buildPrompt(iterationLabel) {
  return `Resume en 4 puntos por qué conviene usar un benchmark para comparar stacks de IA. Iteración ${iterationLabel}. Marca única ${Date.now()}-${Math.random().toString(36).slice(2, 8)}.`;
}

if (!apiKey) {
  throw new Error('Falta OPENROUTER_API_KEY en el entorno');
}

const openrouter = createOpenRouter({ apiKey });

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

function formatMs(value) {
  return `${value.toFixed(1)}ms`;
}

async function runCurrentFastApi(iteration, prompt) {
  const started = performance.now();
  const response = await fetch(`${baseUrl}/api/v1/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      system_prompt: 'Eres un asistente breve y técnico. Responde en español con máximo 6 líneas.',
      message: prompt,
      model,
      mcp_servers: [],
      conversation_id: `bench-current-${Date.now()}-${iteration}`,
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

async function runVercelAiSdk(prompt) {
  const started = performance.now();
  const result = await generateText({
    model: openrouter.chat(model),
    system: 'Eres un asistente breve y técnico. Responde en español con máximo 6 líneas.',
    prompt,
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
  const currentFastApi = [];
  const vercelAiSdk = [];

  console.log('Benchmark iniciado');
  console.log(`Model: ${model}`);
  console.log(`Iterations: ${iterations}`);
  console.log(`FastAPI URL: ${baseUrl}`);
  console.log('');

  const warmupPrompt = buildPrompt('warmup');
  console.log('Warm-up no medido...');
  await runCurrentFastApi(0, warmupPrompt);
  await runVercelAiSdk(warmupPrompt);
  console.log('Warm-up completado');
  console.log('');

  for (let i = 1; i <= iterations; i += 1) {
    const prompt = buildPrompt(i);

    const current = await runCurrentFastApi(i, prompt);
    const vercel = await runVercelAiSdk(prompt);

    currentFastApi.push(current);
    vercelAiSdk.push(vercel);

    console.log(`Iteración ${i}`);
    console.log(`  FastAPI current   -> roundtrip=${formatMs(current.roundtripMs)} | server_total=${formatMs(current.serverTotalMs)} | llm=${formatMs(current.serverLlmMs)} | chars=${current.responseLength}`);
    console.log(`  Vercel AI SDK     -> roundtrip=${formatMs(vercel.roundtripMs)} | finish=${vercel.finishReason} | chars=${vercel.responseLength}`);
    console.log('');
  }

  const currentRoundtrip = stats(currentFastApi.map(item => item.roundtripMs));
  const currentServer = stats(currentFastApi.map(item => item.serverTotalMs));
  const currentLlm = stats(currentFastApi.map(item => item.serverLlmMs));
  const vercelRoundtrip = stats(vercelAiSdk.map(item => item.roundtripMs));

  console.log('=== RESUMEN ===');
  console.log(`FastAPI current roundtrip -> avg=${formatMs(currentRoundtrip.avg)} | p50=${formatMs(currentRoundtrip.p50)} | min=${formatMs(currentRoundtrip.min)} | max=${formatMs(currentRoundtrip.max)}`);
  console.log(`FastAPI server total      -> avg=${formatMs(currentServer.avg)} | p50=${formatMs(currentServer.p50)} | min=${formatMs(currentServer.min)} | max=${formatMs(currentServer.max)}`);
  console.log(`FastAPI llm only          -> avg=${formatMs(currentLlm.avg)} | p50=${formatMs(currentLlm.p50)} | min=${formatMs(currentLlm.min)} | max=${formatMs(currentLlm.max)}`);
  console.log(`Vercel AI SDK roundtrip   -> avg=${formatMs(vercelRoundtrip.avg)} | p50=${formatMs(vercelRoundtrip.p50)} | min=${formatMs(vercelRoundtrip.min)} | max=${formatMs(vercelRoundtrip.max)}`);
  console.log('');
  console.log(`Delta avg (FastAPI roundtrip - Vercel AI SDK) = ${formatMs(currentRoundtrip.avg - vercelRoundtrip.avg)}`);
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
