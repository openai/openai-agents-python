"use client"

import React, { useEffect, useState } from 'react'
import Link from 'next/link'

export default function VariationsPage({ params }: { params: { id: string } }) {
  const { id } = params
  const [loading, setLoading] = useState(true)
  const [campaign, setCampaign] = useState<any | null>(null)
  const [assets, setAssets] = useState<any[]>([])
  const [variations, setVariations] = useState<any[]>([])
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    setLoading(true)
    setError(null)
    Promise.all([
      fetch(`/api/campaigns/${id}`).then((r) => r.json()),
      fetch(`/api/campaigns/${id}/variations`).then((r) => r.json()),
    ])
      .then(([cresp, vres]) => {
        if (!cresp?.success) throw new Error(cresp?.error || 'failed to fetch campaign')
        if (!vres?.success) throw new Error(vres?.error || 'failed to fetch variations')
        setCampaign(cresp.campaign)
        setAssets(cresp.assets || [])
        setVariations(vres.variations || [])
      })
      .catch((e: any) => setError(e?.message || String(e)))
      .finally(() => setLoading(false))
  }, [id])

  async function createVariation() {
    setError(null)
    if (!campaign) return setError('campaign not loaded')
    setCreating(true)
    try {
      // For now, create a placeholder variation that picks first image as preview if any
      const preview = assets[0]?.file_url || null
      const res = await fetch(`/api/campaigns/${id}/variations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: `Var ${new Date().toLocaleString()}`, previewUrl: preview }) })
      const json = await res.json()
      if (!json?.success) throw new Error(json?.error || 'create failed')
      setVariations((s) => [json.variation, ...s])
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setCreating(false)
    }
  }

  async function removeVariation(idToDelete: string) {
    setError(null)
    try {
      const res = await fetch(`/api/variations/${idToDelete}`, { method: 'DELETE' })
      const json = await res.json()
      if (!json?.success) throw new Error(json?.error || 'delete failed')
      setVariations((s) => s.filter((v) => v.id !== idToDelete))
    } catch (e: any) {
      setError(e?.message || String(e))
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm text-slate-500">Create variations for different audiences, sizes and channels.</div>
          <div className="text-xs text-slate-400 mt-1">Campaign: <span className="font-medium">{campaign?.name || id}</span></div>
        </div>
        <div className="flex items-center gap-3 text-sm">
          <Link href={`/campaigns/${id}/formats`} className="px-3 py-2 border rounded">Back: Formats</Link>
          <Link href={`/campaigns/${id}/select-variation`} className={`px-3 py-2 border rounded ${variations.length === 0 ? 'opacity-50 pointer-events-none' : 'bg-indigo-600 text-white'}`}>Next: Select variation</Link>
        </div>
      </div>

      {loading && <div className="text-sm text-slate-500">Loading…</div>}
      {error && <div className="text-sm text-red-600">{error}</div>}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="col-span-2">
          <div className="bg-white p-4 rounded border">
            <div className="flex items-center justify-between">
              <div className="font-semibold">Variations</div>
              <div className="text-sm">
                <button onClick={createVariation} className={`px-3 py-1 rounded border ${creating ? 'opacity-50 pointer-events-none' : 'bg-indigo-600 text-white'}`}>{creating ? 'Creating…' : 'Create Variation (placeholder)'}</button>
              </div>
            </div>

            {variations.length === 0 ? (
              <div className="mt-6 text-sm text-slate-400">No variations yet. Click create to add a placeholder variation.</div>
            ) : (
              <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3">
                {variations.map((v) => (
                  <div key={v.id} className="border rounded p-3 bg-white">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="font-semibold">{v.name}</div>
                        <div className="text-xs text-slate-400">{new Date(v.created_at).toLocaleString()}</div>
                      </div>
                      <div className="flex items-center gap-2 text-sm">
                        <button onClick={() => removeVariation(v.id)} className="text-xs text-red-600 border px-2 py-1 rounded">Delete</button>
                      </div>
                    </div>

                    {v.previewUrl ? (
                      <div className="mt-3 border rounded p-2 flex items-center justify-center bg-slate-50">
                        <img src={v.previewUrl} alt={v.name} style={{ maxHeight: 140 }} />
                      </div>
                    ) : (
                      <div className="mt-3 text-xs text-slate-400">No preview available</div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div>
          <div className="bg-white p-4 rounded border">
            <div className="font-semibold mb-2">Campaign assets</div>
            <div className="text-xs text-slate-500 mb-2">These assets were uploaded for this campaign and can be used as previews for generated variations.</div>

            <div className="space-y-3">
              {assets.length === 0 && <div className="text-xs text-slate-400">No uploaded assets</div>}
              {assets.map((a) => (
                <div key={a.id} className="border p-2 rounded flex items-center gap-3">
                  <div style={{ width: 56, height: 56, overflow: 'hidden' }} className="bg-slate-50 rounded">
                    <img src={a.file_url} alt={a.file_url} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                  </div>
                  <div className="text-xs text-slate-600">{a.type.toUpperCase()} • {a.file_type}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
