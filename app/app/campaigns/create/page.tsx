"use client"

import React, { useRef, useState } from 'react'
import Link from 'next/link'

type Asset = { id: string; url: string }

const ACCEPTED = ['image/png', 'image/jpg', 'image/jpeg', 'image/webp', 'image/svg+xml']
const MAX_BYTES = 20 * 1024 * 1024

export default function CreateCampaign() {
  const [name, setName] = useState('')
  const [client, setClient] = useState('')
  const [campaignId, setCampaignId] = useState<string | null>(null)
  const [logo, setLogo] = useState<Asset | null>(null)
  const [images, setImages] = useState<Asset[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loadingCreate, setLoadingCreate] = useState(false)
  const logoRef = useRef<HTMLInputElement | null>(null)
  const imagesRef = useRef<HTMLInputElement | null>(null)

  async function createCampaign() {
    setError(null)
    if (!name) return setError('Campaign name is required')
    setLoadingCreate(true)
    try {
      const res = await fetch('/api/campaigns', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, client }) })
      const json = await res.json()
      if (!json?.success) throw new Error(json?.error || 'Failed to create')
      setCampaignId(json.campaignId)
    } catch (e: any) {
      setError(e.message || String(e))
    } finally {
      setLoadingCreate(false)
    }
  }

  async function upload(type: 'logo' | 'image', file: File) {
    setError(null)
    if (!campaignId) return setError('Create or select a campaign first')
    if (!ACCEPTED.includes(file.type)) return setError('Unsupported file type')
    if (file.size > MAX_BYTES) return setError('File too large (max 20MB)')

    try {
      const fd = new FormData()
      fd.append('campaignId', campaignId)
      fd.append('file', file, file.name)
      const endpoint = type === 'logo' ? '/api/upload/logo' : '/api/upload/image'
      const res = await fetch(endpoint, { method: 'POST', body: fd })
      const json = await res.json()
      if (!json?.success) throw new Error(json?.error || 'Upload failed')

      const asset = { id: json.asset_id, url: json.file_url }
      if (type === 'logo') setLogo(asset)
      else setImages((s) => [asset, ...s])
    } catch (e: any) {
      setError(e.message || String(e))
    }
  }

  async function handleLogoInput(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (file) upload('logo', file)
    e.currentTarget.value = ''
  }

  async function handleImagesInput(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files || [])
    for (const f of files) {
      await upload('image', f)
    }
    e.currentTarget.value = ''
  }

  function onDropFactory(type: 'logo' | 'image') {
    return async function (e: React.DragEvent<HTMLDivElement>) {
      e.preventDefault()
      if (!campaignId) return setError('Create or select a campaign first')
      const items = Array.from(e.dataTransfer.files || [])
      for (const f of items) await upload(type, f)
    }
  }

  async function removeAsset(type: 'logo' | 'image', id: string) {
    setError(null)
    try {
      const endpoint = type === 'logo' ? `/api/upload/logo?id=${encodeURIComponent(id)}` : `/api/upload/image?id=${encodeURIComponent(id)}`
      const res = await fetch(endpoint, { method: 'DELETE' })
      const json = await res.json()
      if (!json?.success) throw new Error(json?.error || 'Delete failed')
      if (type === 'logo') setLogo(null)
      else setImages((s) => s.filter((a) => a.id !== id))
    } catch (e: any) {
      setError(e.message || String(e))
    }
  }

  const nextDisabled = !logo || images.length < 1 || !campaignId

  return (
    <div className="max-w-4xl mx-auto bg-white p-6 rounded shadow">
      <h2 className="text-xl font-semibold">Create a new Campaign</h2>
      <p className="mt-2 text-sm text-slate-500">Fill the basic info first — a campaign record is created immediately and files can be uploaded to a dedicated folder on disk.</p>

      <div className="mt-6 grid gap-4">
        <label className="block">
          <div className="text-sm font-medium">Campaign name</div>
          <input value={name} onChange={(e) => setName(e.target.value)} type="text" className="mt-1 w-full border rounded px-3 py-2" placeholder="E.g. Spring Social Launch" />
        </label>

        <label className="block">
          <div className="text-sm font-medium">Client name</div>
          <input value={client} onChange={(e) => setClient(e.target.value)} type="text" className="mt-1 w-full border rounded px-3 py-2" placeholder="Brand or client" />
        </label>

        <div className="flex items-center justify-end gap-3 mt-2">
          <Link href="/campaigns" className="px-3 py-2 border rounded">Cancel</Link>
          {!campaignId ? (
            <button onClick={createCampaign} className="px-3 py-2 bg-indigo-600 text-white rounded" disabled={loadingCreate}>{loadingCreate ? 'Creating…' : 'Create campaign'}</button>
          ) : (
            <div className="text-sm text-slate-500">Campaign created: <span className="font-medium">{campaignId}</span></div>
          )}
        </div>

        {/* Upload areas */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mt-6">
          <div>
            <div className="text-sm font-semibold mb-2">Logo (single)</div>

            <div onDrop={onDropFactory('logo')} onDragOver={(e) => e.preventDefault()} className="border-dashed border-2 rounded p-4 text-center">
              <div className="text-xs text-slate-500">PNG, JPG, JPEG, WEBP, SVG — max 20MB</div>
              <div className="mt-3 flex items-center justify-center gap-2">
                <button onClick={() => logoRef.current?.click()} className="px-3 py-2 border rounded">Click to upload</button>
                <div className="text-sm text-slate-400">or drag & drop a single logo file here</div>
              </div>

              <input ref={logoRef} type="file" accept="image/*" onChange={handleLogoInput} className="hidden" />
            </div>

            {logo && (
              <div className="mt-4">
                <div className="text-xs text-slate-500">Preview</div>
                <div className="mt-2 border rounded p-4 w-full flex items-center justify-center bg-slate-50">
                  <img src={logo.url} alt="logo" style={{ maxHeight: 140 }} />
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <button onClick={() => removeAsset('logo', logo.id)} className="px-2 py-1 text-sm border rounded text-red-600">Remove</button>
                </div>
              </div>
            )}
          </div>

          <div>
            <div className="text-sm font-semibold mb-2">Images (multiple)</div>

            <div onDrop={onDropFactory('image')} onDragOver={(e) => e.preventDefault()} className="border-dashed border-2 rounded p-4 text-center">
              <div className="text-xs text-slate-500">PNG, JPG, JPEG, WEBP, SVG — max 20MB per file</div>
              <div className="mt-3 flex items-center justify-center gap-2">
                <button onClick={() => imagesRef.current?.click()} className="px-3 py-2 border rounded">Click to upload</button>
                <div className="text-sm text-slate-400">or drag & drop multiple images</div>
              </div>
              <input multiple ref={imagesRef} type="file" accept="image/*" onChange={handleImagesInput} className="hidden" />
            </div>

            {images.length > 0 && (
              <div className="mt-4">
                <div className="text-xs text-slate-500">Previews</div>
                <div className="mt-2 grid grid-cols-3 gap-2">
                  {images.map((a) => (
                    <div key={a.id} className="border rounded p-2 flex flex-col items-center">
                      <img src={a.url} alt="img" className="object-cover h-20 w-full" />
                      <div className="mt-2 flex items-center gap-2">
                        <button onClick={() => removeAsset('image', a.id)} className="text-xs border px-2 py-1 rounded text-red-600">Remove</button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        {error && <div className="mt-4 text-sm text-red-600">{error}</div>}

        <div className="mt-6 flex items-center justify-end gap-3">
          <Link href="/campaigns" className="px-3 py-2 border rounded">Back to campaigns</Link>
          <Link href={campaignId ? `/campaigns/${campaignId}/formats` : '#'} className={`px-3 py-2 bg-indigo-600 text-white rounded ${nextDisabled ? 'opacity-50 pointer-events-none' : ''}`}>Next: Choose Formats</Link>
        </div>
      </div>
    </div>
  )
}
