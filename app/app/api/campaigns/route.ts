import { NextResponse } from 'next/server'
import { insertCampaign } from '../../../lib/db'

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}))
  const { name, client } = body
  if (!name) {
    return NextResponse.json({ success: false, error: 'Missing campaign name' }, { status: 400 })
  }

  const id = crypto.randomUUID()
  const created_at = new Date().toISOString()

  insertCampaign({ id, name, client, status: 'draft', created_at })

  return NextResponse.json({ success: true, campaignId: id })
}
