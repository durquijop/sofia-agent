import 'dotenv/config';
import { performance } from 'node:perf_hooks';
import { createOpenRouter } from '@openrouter/ai-sdk-provider';
import { generateText, stepCountIs, tool } from 'ai';
import { z } from 'zod';

const apiKey = process.env.OPENROUTER_API_KEY;
const model = process.env.BENCH_MODEL || 'x-ai/grok-4.1-fast';
const iterations = Number(process.env.BENCH_ITERATIONS || 3);
const maxTokens = Number(process.env.BENCH_MAX_TOKENS || 220);
const temperature = Number(process.env.BENCH_TEMPERATURE || 0);

if (!apiKey) {
  throw new Error('Falta OPENROUTER_API_KEY');
}

const openrouter = createOpenRouter({ apiKey });

function formatMs(value) {
  return `${value.toFixed(1)}ms`;
}

function stats(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const sum = sorted.reduce((acc, value) => acc + value, 0);
  const avg = sum / sorted.length;
  const mid = Math.floor(sorted.length / 2);
  const p50 = sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
  return { min: sorted[0], max: sorted[sorted.length - 1], avg, p50 };
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function buildAgentSpec(agentName) {
  if (agentName === 'funnel') {
    return {
      system: 'Eres un agente de embudo. Debes llamar get_funnel_rules y get_contact_signals antes de responder. Si es útil, también puedes llamar get_recent_activity. Responde en máximo 3 líneas para el equipo interno.',
      prompt: 'Analiza el lead 328159. Debes usar get_funnel_rules y get_contact_signals antes de concluir. Si existe una señal reciente importante, consulta get_recent_activity también. No inventes datos.',
      toolsFactory(metrics) {
        return {
          get_funnel_rules: tool({
            description: 'Obtiene reglas del embudo y criterios para clasificar la etapa actual.',
            inputSchema: z.object({ leadId: z.number() }),
            async execute({ leadId }) {
              const started = performance.now();
              await sleep(240);
              const result = {
                leadId,
                availableStages: ['nuevo', 'interesado', 'calificado', 'cita'],
                currentStageHint: 'interesado',
                requiredFields: ['presupuesto', 'necesidad', 'canal_origen'],
              };
              metrics.push({ name: 'get_funnel_rules', ms: performance.now() - started });
              return result;
            },
          }),
          get_contact_signals: tool({
            description: 'Obtiene señales recientes del lead como intención, presupuesto y urgencia.',
            inputSchema: z.object({ leadId: z.number() }),
            async execute({ leadId }) {
              const started = performance.now();
              await sleep(360);
              const result = {
                leadId,
                intent: 'alta',
                budget: 'medio',
                urgency: 'esta_semana',
                asksForPricing: true,
              };
              metrics.push({ name: 'get_contact_signals', ms: performance.now() - started });
              return result;
            },
          }),
          get_recent_activity: tool({
            description: 'Obtiene actividad reciente del lead y del equipo comercial.',
            inputSchema: z.object({ leadId: z.number() }),
            async execute({ leadId }) {
              const started = performance.now();
              await sleep(180);
              const result = {
                leadId,
                lastInboundMinutesAgo: 12,
                askedForDemo: true,
                hasAssignedAdvisor: false,
              };
              metrics.push({ name: 'get_recent_activity', ms: performance.now() - started });
              return result;
            },
          }),
        };
      },
    };
  }

  return {
    system: 'Eres un agente procesador. Debes llamar get_processing_policy y get_conversation_summary antes de responder. Si hace falta, llama get_followup_constraints. Responde en máximo 3 líneas para el equipo interno.',
    prompt: 'Prepara la siguiente acción para el lead 328159. Debes usar get_processing_policy y get_conversation_summary antes de concluir. Si hay restricciones horarias o de canal, usa get_followup_constraints.',
    toolsFactory(metrics) {
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
            metrics.push({ name: 'get_processing_policy', ms: performance.now() - started });
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
            metrics.push({ name: 'get_conversation_summary', ms: performance.now() - started });
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
            metrics.push({ name: 'get_followup_constraints', ms: performance.now() - started });
            return result;
          },
        }),
      };
    },
  };
}

async function runAgent(agentName, iteration) {
  const spec = buildAgentSpec(agentName);
  const toolMetrics = [];
  const tools = spec.toolsFactory(toolMetrics);
  const started = performance.now();

  const result = await generateText({
    model: openrouter.chat(model),
    system: `${spec.system}\nEjecución benchmark: ${agentName} iteración ${iteration}. Si puedes, llama múltiples herramientas antes de responder.`,
    prompt: spec.prompt,
    tools,
    toolChoice: 'required',
    stopWhen: stepCountIs(6),
    maxTokens,
    temperature,
  });

  const totalMs = performance.now() - started;
  const allToolCalls = result.steps.flatMap(step => step.toolCalls ?? []);
  const uniqueTools = [...new Set(allToolCalls.map(call => call.toolName))];
  const toolMs = toolMetrics.reduce((acc, item) => acc + item.ms, 0);

  return {
    agentName,
    totalMs,
    toolMs,
    toolCalls: allToolCalls.length,
    uniqueTools,
    steps: result.steps.length,
    responseLength: String(result.text || '').length,
  };
}

async function runWorkflow(iteration) {
  const started = performance.now();
  const [funnel, processor] = await Promise.all([
    runAgent('funnel', iteration),
    runAgent('processor', iteration),
  ]);
  const workflowMs = performance.now() - started;
  return { workflowMs, funnel, processor };
}

async function main() {
  const runs = [];

  console.log('Benchmark Vercel AI SDK - agentes paralelos');
  console.log(`Model: ${model}`);
  console.log(`Iterations: ${iterations}`);
  console.log('');

  console.log('Warm-up no medido...');
  await runWorkflow(0);
  console.log('Warm-up completado');
  console.log('');

  for (let i = 1; i <= iterations; i += 1) {
    const run = await runWorkflow(i);
    runs.push(run);

    console.log(`Iteración ${i}`);
    console.log(`  Workflow total     -> ${formatMs(run.workflowMs)}`);
    console.log(`  Funnel agent       -> total=${formatMs(run.funnel.totalMs)} | tools=${formatMs(run.funnel.toolMs)} | steps=${run.funnel.steps} | tool_calls=${run.funnel.toolCalls} | unique_tools=${run.funnel.uniqueTools.join(',')}`);
    console.log(`  Processor agent    -> total=${formatMs(run.processor.totalMs)} | tools=${formatMs(run.processor.toolMs)} | steps=${run.processor.steps} | tool_calls=${run.processor.toolCalls} | unique_tools=${run.processor.uniqueTools.join(',')}`);
    console.log('');
  }

  const workflowStats = stats(runs.map(run => run.workflowMs));
  const funnelStats = stats(runs.map(run => run.funnel.totalMs));
  const processorStats = stats(runs.map(run => run.processor.totalMs));
  const funnelToolStats = stats(runs.map(run => run.funnel.toolMs));
  const processorToolStats = stats(runs.map(run => run.processor.toolMs));

  console.log('=== RESUMEN ===');
  console.log(`Workflow total    -> avg=${formatMs(workflowStats.avg)} | p50=${formatMs(workflowStats.p50)} | min=${formatMs(workflowStats.min)} | max=${formatMs(workflowStats.max)}`);
  console.log(`Funnel total      -> avg=${formatMs(funnelStats.avg)} | p50=${formatMs(funnelStats.p50)} | min=${formatMs(funnelStats.min)} | max=${formatMs(funnelStats.max)}`);
  console.log(`Processor total   -> avg=${formatMs(processorStats.avg)} | p50=${formatMs(processorStats.p50)} | min=${formatMs(processorStats.min)} | max=${formatMs(processorStats.max)}`);
  console.log(`Funnel tools      -> avg=${formatMs(funnelToolStats.avg)} | p50=${formatMs(funnelToolStats.p50)} | min=${formatMs(funnelToolStats.min)} | max=${formatMs(funnelToolStats.max)}`);
  console.log(`Processor tools   -> avg=${formatMs(processorToolStats.avg)} | p50=${formatMs(processorToolStats.p50)} | min=${formatMs(processorToolStats.min)} | max=${formatMs(processorToolStats.max)}`);
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
