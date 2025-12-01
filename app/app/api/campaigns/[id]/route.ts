import { NextResponse } from 'next/server'
import { findCampaignById, findAssetsByCampaignId, updateCampaign } from '../../../../lib/db'

export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = params.id
  const campaign = findCampaignById(id)
  if (!campaign) return NextResponse.json({ success: false, error: 'not found' }, { status: 404 })
  const assets = findAssetsByCampaignId(id)
  return NextResponse.json({ success: true, campaign, assets })
}

export async function PATCH(req: Request, { params }: { params: { id: string } }) {
  const id = params.id
  const payload = await req.json().catch(() => ({}))

  // Allow updating name, client, status, formats
  const allowed: any = {}
  if (typeof payload.name === 'string') allowed.name = payload.name
  if (typeof payload.client === 'string') allowed.client = payload.client
  if (typeof payload.status === 'string') allowed.status = payload.status
  if (Array.isArray(payload.formats)) allowed.formats = payload.formats

  const updated = updateCampaign(id, allowed)
  if (!updated) return NextResponse.json({ success: false, error: 'not found' }, { status: 404 })
  return NextResponse.json({ success: true, campaign: updated })
}
