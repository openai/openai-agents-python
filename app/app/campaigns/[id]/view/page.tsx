export default function CampaignView({ params }: { params: { id: string } }) {
  return (
    <div className="space-y-4">
      <div className="text-sm text-slate-500">Campaign view — overview of deliverables, status and audit trails.</div>

      <div className="border rounded p-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-semibold">Campaign details</div>
            <div className="text-xs text-slate-500 mt-1">Status: Draft</div>
          </div>
          <div className="text-xs text-slate-400">ID: {params.id}</div>
        </div>
      </div>
    </div>
  )
}
