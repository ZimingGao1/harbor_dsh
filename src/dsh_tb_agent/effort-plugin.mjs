/**
 * External evaluation policy using DSH 0.1.0rc6's public waterfall hooks.
 *
 * Uses the post-next pattern (call the chain, then rewrite the resolved
 * config), matching the assembled-selection listener in the shipped runtime.
 * The step cap applies per turn. Harbor's driver starts one fresh session
 * and submits exactly one prompt; it never resumes or steers this session.
 * HTTP retries and multiple tools within a step do not consume extra steps.
 */

export const name = 'dsh-reasoning-effort'

export function apply(ctx) {
  const maxSteps = Number(process.env.DSH_MAX_STEPS ?? '500')
  if (!Number.isSafeInteger(maxSteps) || maxSteps < 1) {
    throw new Error('DSH_MAX_STEPS must be a positive safe integer')
  }
  ctx.on('agent/pre-step', async (payload, next) => {
    if (payload.step > maxSteps) return { kind: 'reject' }
    return next()
  })
  ctx.on('agent/request', async (_payload, next) => {
    const effort = process.env.DSH_REASONING_EFFORT
    const resolved = await next()
    return {
      ...resolved,
      ...(effort ? { reasoningEffort: effort } : {}),
      ...(process.env.DSH_TEMPERATURE
        ? { temperature: Number(process.env.DSH_TEMPERATURE) }
        : {}),
    }
  })
}
