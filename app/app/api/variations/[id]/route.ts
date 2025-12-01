import { NextResponse } from 'next/server'
import { deleteVariationById } from '../../../../lib/db'

export async function DELETE(req: Request, { params }: { params: { id: string } }) {
  const id = params.id
  const deleted = deleteVariationById(id)
  if (!deleted) return NextResponse.json({ success: false, error: 'not found' }, { status: 404 })
  return NextResponse.json({ success: true })
}
