import { NextResponse } from 'next/server'

/**
 * Placeholder API endpoint for generating a complete campaign.
 * Expected to accept a request that defines campaign specs and returns a job handle.
 */
export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}))

  // No integration here yet — return a fixed placeholder
  return NextResponse.json({
    status: 'accepted',
    message: 'Campaign generation request accepted (placeholder).',
    received: body,
    campaignId: 'camp_placeholder_123'
  })
}
