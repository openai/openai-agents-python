import { NextResponse } from 'next/server'
import { findCampaignById, findVariationsByCampaignId, insertVariation } from '../../../../../lib/db'

export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = params.id
  const campaign = findCampaignById(id)
  if (!campaign) return NextResponse.json({ success: false, error: 'campaign not found' }, { status: 404 })
  const variations = findVariationsByCampaignId(id)
  return NextResponse.json({ success: true, variations })
}

export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = params.id
  const campaign = findCampaignById(id)
  if (!campaign) return NextResponse.json({ success: false, error: 'campaign not found' }, { status: 404 })

  const payload = await req.json().catch(() => ({}))
  const name = payload.name || `Variation ${new Date().toISOString()}`

  const vId = crypto.randomUUID()
  const previewUrl = payload.previewUrl || null
  insertVariation({ id: vId, campaignId: id, name, previewUrl: previewUrl || undefined, created_at: new Date().toISOString() })

  return NextResponse.json({ success: true, variation: { id: vId, campaignId: id, name, previewUrl } })
}
