export default function SelectVariation({ params }: { params: { id: string } }) {
  return (
    <div>
      <div className="text-sm text-slate-500">Select the best variation(s) from the generated pool.</div>

      <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="border rounded p-4">Variation A (preview)</div>
        <div className="border rounded p-4">Variation B (preview)</div>
      </div>
    </div>
  )
}
