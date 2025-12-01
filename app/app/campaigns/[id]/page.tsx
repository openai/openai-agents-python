import Link from 'next/link'

export default function CampaignRoot({ params }: { params: { id: string } }) {
  return (
    <div>
      <div className="p-4 border rounded text-sm text-slate-700">Start your campaign workflow. Use the navigation buttons to move between steps.</div>

      <div className="mt-4 flex gap-3">
        <Link href={`/campaigns/${params.id}/formats`} className="px-3 py-2 bg-indigo-600 text-white rounded">Choose formats</Link>
        <Link href={`/campaigns/${params.id}/variations`} className="px-3 py-2 border rounded">Create variations</Link>
      </div>
    </div>
  )
}
