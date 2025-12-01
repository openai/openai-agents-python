export default function FinalizePage({ params }: { params: { id: string } }) {
  return (
    <div>
      <div className="mb-4 text-sm text-slate-500">Finalize specs and confirm your campaign. This step will later trigger orchestration to downstream systems.</div>

      <div className="border rounded p-4">
        <div className="font-semibold">Deliverables</div>
        <ul className="text-sm text-slate-700 mt-2 list-disc pl-5">
          <li>OOH banner sizes</li>
          <li>Social static + video</li>
          <li>Print ready PDFs</li>
        </ul>
      </div>
    </div>
  )
}
