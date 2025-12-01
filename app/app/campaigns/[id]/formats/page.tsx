"use client"

import React, { useEffect, useState } from 'react'
import Link from 'next/link'

const ALL_FORMATS = [
  { id: 'ooh', label: 'OOH', description: 'Outdoor, billboards and transit.' },
  { id: 'social', label: 'Social', description: 'Static and short video specs.' },
  { id: 'print', label: 'Print', description: 'Magazines and posters.' },
  { id: 'dooh', label: 'DOOH', description: 'Digital out-of-home specific formats.' },
]

export default function FormatsPage({ params }: { params: { id: string } }) {
  const id = params.id
  const [loading, setLoading] = useState(true)
  const [campaign, setCampaign] = useState<any | null>(null)
  const [assets, setAssets] = useState<any[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setLoading(true)
    fetch(`/api/campaigns/${id}`)
      .then((r) => r.json())
      .then((json) => {
        if (!json?.success) throw new Error(json?.error || 'fetch failed')
        setCampaign(json.campaign)
        setAssets(json.assets || [])
        setSelected(json.campaign?.formats || [])
      })
      .catch((e: any) => setError(e?.message || String(e)))
      .finally(() => setLoading(false))
  }, [id])

  function toggleFormat(key: string) {
    setSelected((s) => (s.includes(key) ? s.filter((x) => x !== key) : [...s, key]))
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(`/api/campaigns/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ formats: selected }) })
      const json = await res.json()
      if (!json?.success) throw new Error(json?.error || 'save failed')
      setCampaign(json.campaign)
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setSaving(false)
    }
  }

  const logo = assets.find((a) => a.type === 'logo')
  const images = assets.filter((a) => a.type === 'image')

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm text-slate-500">Choose the creative formats this campaign should generate.</div>
          <div className="text-xs text-slate-400 mt-1">Campaign: <span className="font-medium">{campaign?.name || id}</span></div>
        </div>
        <div className="text-sm">
          <Link href={`/campaigns/${id}/variations`} className={`px-3 py-2 border rounded ${selected.length === 0 ? 'opacity-50 pointer-events-none' : 'bg-indigo-600 text-white'}`}>Next: Variations</Link>
        </div>
      </div>

      {loading && <div className="text-sm text-slate-500">Loading campaign…</div>}
      {error && <div className="text-sm text-red-600">{error}</div>}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="col-span-2">
          <div className="bg-white p-4 rounded border">
            <div className="mb-3 font-semibold">Select formats</div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {ALL_FORMATS.map((f) => (
                <label key={f.id} className={`border rounded p-3 flex items-start gap-3 ${selected.includes(f.id) ? 'ring-2 ring-indigo-200' : ''}`}>
                  <input type="checkbox" checked={selected.includes(f.id)} onChange={() => toggleFormat(f.id)} />
                  <div>
                    <div className="font-semibold">{f.label}</div>
                    <div className="text-xs text-slate-500">{f.description}</div>
                  </div>
                </label>
              ))}
            </div>

            <div className="mt-4 flex items-center gap-3 justify-end">
              <button onClick={save} className={`px-3 py-2 rounded border ${saving ? 'opacity-50 pointer-events-none' : 'bg-indigo-600 text-white'}`}>Save</button>
            </div>
          </div>
        </div>

        <div>
          <div className="bg-white p-4 rounded border">
            <div className="font-semibold mb-2">Uploaded assets</div>
            {logo ? (
              <div className="mb-4">
                <div className="text-xs text-slate-500">Logo</div>
                <div className="mt-2 border rounded p-2 flex items-center justify-center bg-slate-50">
                  <img src={logo.file_url} alt="logo" style={{ maxHeight: 140 }} />
                </div>
              </div>
            ) : (
              <div className="text-xs text-slate-400 mb-4">No logo uploaded</div>
            )}

            <div>
              <div className="text-xs text-slate-500">Images</div>
              {images.length === 0 ? (
                <div className="mt-2 text-xs text-slate-400">No images uploaded</div>
              ) : (
                <div className="mt-2 grid grid-cols-2 gap-2">
                  {images.map((a) => (
                    <div key={a.id} className="border rounded p-1 flex items-center justify-center">
                      <img src={a.file_url} alt="img" style={{ width: '100%', height: 80, objectFit: 'cover' }} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
