import React from 'react'
import Link from 'next/link'

export default function CampaignLayout({ children, params }: { children: React.ReactNode; params: { id: string } }) {
  const { id } = params

  return (
    <div className="max-w-6xl mx-auto">
      <div className="bg-white shadow rounded-md p-6">
        <div className="flex items-start justify-between gap-6">
          <div>
            <div className="text-sm text-slate-500">Campaign</div>
            <div className="text-lg font-semibold">{id} — Campaign workflow</div>
          </div>
          <div className="flex items-center gap-3 text-sm text-slate-600">
            <Link href={`/campaigns/${id}/formats`} className="px-3 py-2 border rounded">Formats</Link>
            <Link href={`/campaigns/${id}/variations`} className="px-3 py-2 border rounded">Variations</Link>
            <Link href={`/campaigns/${id}/final`} className="px-3 py-2 border rounded">Finalize</Link>
            <Link href={`/campaigns/${id}/view`} className="px-3 py-2 bg-indigo-600 text-white rounded">View</Link>
          </div>
        </div>

        <div className="mt-6">{children}</div>
      </div>
    </div>
  )
}
