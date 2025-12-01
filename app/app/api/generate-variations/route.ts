import { NextResponse } from 'next/server'

/**
 * Placeholder API endpoint for generating variations.
 * In the future this will enqueue a job and call out to external n8n workflows, return a job id and status.
 * For now it returns a stubbed response showing the expected contract.
 */
export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}))

  // This is intentionally a placeholder
  return NextResponse.json({
    status: 'queued',
    message: 'Variation generation request received (placeholder).',
    received: body,
    jobId: 'job_placeholder_123'
  })
}
