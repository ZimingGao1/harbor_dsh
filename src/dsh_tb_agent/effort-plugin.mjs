/**
 * Agent/request waterfall plugin: propose `reasoningEffort` for every model
 * request from the DSH_REASONING_EFFORT environment variable.
 *
 * Uses the post-next pattern (call the chain, then rewrite the resolved
 * config), matching the assembled-selection listener in the shipped runtime.
 * The plugin is inert when DSH_REASONING_EFFORT is unset.
 */

export const name = 'dsh-reasoning-effort'

export function apply(ctx) {
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
