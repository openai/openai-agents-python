import Link from 'next/link'

export default function Campaigns() {
  // Placeholder list view for campaigns
  const sample = [
    { id: 'abc123', name: 'Summer OOH Push' },
    { id: 'xyz789', name: 'Holiday Social Teaser' },
  ]

  return (
    <div className="max-w-6xl mx-auto">
      <div className="bg-white shadow rounded-md p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Campaigns</h2>
          <Link href="/campaigns/create" className="text-indigo-600 font-semibold">Create new</Link>
        </div>

        <ul className="mt-5 divide-y">
          {sample.map((c) => (
            <li key={c.id} className="py-3 flex items-center justify-between">
              <div>
                <div className="font-medium">{c.name}</div>
                <div className="text-xs text-slate-500">ID: {c.id}</div>
              </div>
              <div className="flex items-center gap-3">
                <Link href={`/campaigns/${c.id}/view`} className="text-sm text-indigo-600">View</Link>
                <Link href={`/campaigns/${c.id}/formats`} className="text-sm text-slate-600">Edit</Link>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
