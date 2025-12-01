import Link from 'next/link'

export default function CampaignCard({ id, name, meta }: { id: string; name: string; meta?: string }) {
  return (
    <article className="border rounded p-4 shadow-sm hover:shadow-md">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-semibold">{name}</div>
          {meta && <div className="text-xs text-slate-500 mt-1">{meta}</div>}
        </div>
        <div className="text-sm flex items-center gap-2">
          <Link href={`/campaigns/${id}/view`} className="text-indigo-600">View</Link>
          <Link href={`/campaigns/${id}/formats`} className="text-slate-600">Edit</Link>
        </div>
      </div>
    </article>
  )
}
