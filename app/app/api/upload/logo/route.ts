import { NextResponse } from 'next/server'
import { insertAsset, deleteAssetById, findCampaignById } from '../../../../lib/db'
import { saveFile, deleteFile } from '../../../../lib/storage'

const MAX_BYTES = 20 * 1024 * 1024 // 20MB
const ALLOWED = ['.png', '.jpg', '.jpeg', '.webp', '.svg']

function extFromFilename(name: string) {
  const idx = name.lastIndexOf('.')
  return idx === -1 ? '' : name.slice(idx).toLowerCase()
}

export async function POST(req: Request) {
  const fd = await req.formData()
  const campaignId = fd.get('campaignId')?.toString() || ''
  const file = fd.get('file') as File | null

  if (!campaignId) return NextResponse.json({ success: false, error: 'campaignId required' }, { status: 400 })
  const campaign = findCampaignById(campaignId)
  if (!campaign) return NextResponse.json({ success: false, error: 'campaign not found' }, { status: 404 })

  if (!file) return NextResponse.json({ success: false, error: 'file required' }, { status: 400 })

  const filename = (file as any).name || 'upload'
  const ext = extFromFilename(filename)
  if (!ALLOWED.includes(ext)) return NextResponse.json({ success: false, error: 'file type not allowed' }, { status: 400 })

  const buffer = Buffer.from(await (file as File).arrayBuffer())
  if (buffer.byteLength > MAX_BYTES) return NextResponse.json({ success: false, error: 'file too large' }, { status: 400 })

  const unique = `${crypto.randomUUID()}${ext}`
  const url = saveFile(buffer, campaignId, 'logo', unique)

  const assetId = crypto.randomUUID()
  insertAsset({ id: assetId, campaignId, type: 'logo', file_url: url, file_type: ext, created_at: new Date().toISOString() })

  return NextResponse.json({ success: true, file_url: url, asset_id: assetId })
}

export async function DELETE(req: Request) {
  const url = new URL(req.url)
  const id = url.searchParams.get('id')
  if (!id) return NextResponse.json({ success: false, error: 'id required' }, { status: 400 })

  const asset = deleteAssetById(id)
  if (!asset) return NextResponse.json({ success: false, error: 'asset not found' }, { status: 404 })

  // delete file
  const fileName = asset.file_url.split('/').pop() || ''
  deleteFile(asset.campaignId, 'logo', fileName)

  return NextResponse.json({ success: true })
}
